// Optional browser-logic tests for static/app.js (needs Node 20+ and `npm install` here).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../static");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "app.js"), "utf8");
const tick = () => new Promise((r) => setImmediate(r));

// A page with a fake clock and a scriptable fake server.
function setup(handlers) {
  const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  let now = 1_000_000, nextId = 1;
  const timers = new Map();
  const calls = [];
  w.Date.now = () => now;
  w.setTimeout = (fn, ms) => { const id = nextId++; timers.set(id, { fn, at: now + ms }); return id; };
  w.clearTimeout = (id) => timers.delete(id);
  w.fetch = async (url, opts) => {
    calls.push(url);
    const out = await handlers(url, opts);
    if (out instanceof Error) throw out;
    const { status = 200, body } = out;
    return { ok: status < 400, status, json: async () => { if (body === undefined) throw new Error("no json"); return body; } };
  };
  w.eval(script);
  const $ = (id) => w.document.getElementById(id);
  const advance = async (ms) => {
    const end = now + ms;
    for (;;) {
      await tick();
      const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      now = Math.max(now, due[1].at); timers.delete(due[0]); due[1].fn();
    }
    now = end; await tick();
  };
  const submit = async (amount = "100", phone = "0712345678") => {
    $("amount").value = amount; $("phone").value = phone;
    $("paymentForm").dispatchEvent(new w.Event("submit", { cancelable: true }));
    await tick(); await tick();
  };
  return { $, calls, advance, submit, state: () => $("status").getAttribute("data-state") };
}

const payOk = { status: 200, body: { success: true, message: "Check your phone and enter your M-Pesa PIN.", checkoutRequestId: "ws_CO_123456789" } };
const poller = (...seq) => (url, opts) => {
  if (url === "/api/pay") return payOk;
  return seq.length > 1 ? seq.shift() : seq[0];
};
const st = (status, extra = {}) => ({ status: 200, body: { success: true, status, message: status + " msg", ...extra } });

test("invalid phone and amount never reach the server", async () => {
  const p = setup(poller(st("PENDING")));
  await p.submit("100", "12345");
  assert.equal(p.state(), "error"); assert.equal(p.calls.length, 0);
  await p.submit("0", "0712345678");
  assert.equal(p.state(), "error"); assert.equal(p.calls.length, 0);
  await p.submit("1.5", "0712345678");
  assert.equal(p.calls.length, 0);
});

test("accepted request shows PENDING, not success, and keeps button disabled", async () => {
  const p = setup(poller(st("PENDING")));
  await p.submit();
  assert.equal(p.state(), "pending");
  assert.equal(p.$("payButton").disabled, true);
  await p.advance(9000);
  assert.equal(p.state(), "pending");
});

test("success appears only after backend reports SUCCESS, with receipt", async () => {
  const p = setup(poller(st("PENDING"), st("SUCCESS", { receipt: "NLJ7RT61SV" })));
  await p.submit();
  assert.equal(p.state(), "pending");
  await p.advance(3100); assert.equal(p.state(), "pending");
  await p.advance(3100);
  assert.equal(p.state(), "success");
  assert.match(p.$("statusMessage").textContent, /NLJ7RT61SV/);
  assert.equal(p.$("payButton").disabled, false);
});

test("cancelled and failed are shown as different states", async () => {
  let p = setup(poller(st("CANCELLED"))); await p.submit(); await p.advance(3100);
  assert.equal(p.state(), "cancelled");
  p = setup(poller(st("FAILED"))); await p.submit(); await p.advance(3100);
  assert.equal(p.state(), "failed");
});

test("timeout message, then Check again can find the result", async () => {
  const seq = [st("PENDING")];
  const p = setup((url) => url === "/api/pay" ? payOk : seq[0]);
  await p.submit();
  await p.advance(125000);
  assert.equal(p.state(), "timeout");
  assert.equal(p.$("checkAgain").classList.contains("hidden"), false);
  assert.equal(p.$("payButton").disabled, false);
  seq[0] = st("SUCCESS", { receipt: "AB12CD34EF" });
  p.$("checkAgain").click(); await p.advance(3100);
  assert.equal(p.state(), "success");
});

test("server errors, unknown payment and network loss are handled", async () => {
  let p = setup((url) => url === "/api/pay" ? { status: 502, body: { success: false, message: "Provider down" } } : st("PENDING"));
  await p.submit();
  assert.equal(p.state(), "error"); assert.match(p.$("statusMessage").textContent, /Provider down/);
  assert.equal(p.$("payButton").disabled, false);

  p = setup((url) => url === "/api/pay" ? payOk : { status: 404, body: { success: false, status: "UNKNOWN" } });
  await p.submit(); await p.advance(3100);
  assert.equal(p.state(), "error");

  p = setup((url) => url === "/api/pay" ? payOk : new Error("offline"));
  await p.submit(); await p.advance(20000);
  assert.equal(p.state(), "error"); assert.equal(p.$("payButton").disabled, false);

  p = setup(() => new Error("offline"));
  await p.submit();
  assert.equal(p.state(), "error");
});

test("server text is inserted as text, never as HTML", async () => {
  const evil = '<img src=x onerror="window.pwned=1">';
  const p = setup(poller(st("FAILED", { message: evil })));
  await p.submit(); await p.advance(3100);
  assert.equal(p.$("statusMessage").textContent, evil);
  assert.equal(p.$("statusMessage").querySelector("img"), null);
});
