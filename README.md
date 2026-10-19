# M-PESA STK Push Demo (Daraja Sandbox)

A small Flask app that sends an M-PESA STK Push prompt to a phone and then shows
the **real** result (success, cancelled or failed) on the page. It runs on
Render's free web service and keeps its records in Neon's free Postgres database.

> **Sandbox only.** This is a learning/demo project. It is not a production
> payment system. See [Limitations](#14-limitations-and-what-production-would-need).

---

## 1. How it works (read this once)

```
 Browser ──POST /api/pay──────────────► Flask on Render ──► Daraja Sandbox ──► your phone
    ▲                                        │  saves a PENDING row            │
    │                                        ▼                                 │ you enter PIN / cancel
    │                                  Neon Postgres  ◄── POST /api/callback/<token> ◄── Safaricom
    └──GET /api/payment-status/<id>──────────┘   (browser polls every 3 s)
```

1. The page sends the amount and phone to `/api/pay`.
2. The server asks Daraja to push the prompt. **Daraja accepting the request does
   not mean the customer paid.** The server stores a `PENDING` record.
3. The customer enters a PIN or cancels. Safaricom then POSTs the outcome to the
   `CallBackURL` we sent with the request. That URL is **our public Render URL**,
   which is why a laptop plus a temporary tunnel is no longer needed.
4. The server updates the record **only if it is still PENDING**. The page polls
   `/api/payment-status/<id>` and only shows "successful" when the stored record
   says `SUCCESS`.
5. If no callback arrives within 30 seconds, the status endpoint asks Daraja's
   *STK Push Query* API directly (at most once every 15 seconds). A missing
   callback is **never** treated as success.

### Result codes

| Daraja `ResultCode` | Stored status | Meaning |
|---|---|---|
| `0` | `SUCCESS` | Reported successful (receipt number stored) |
| `1032` | `CANCELLED` | Customer cancelled |
| anything else (`1`, `2001`, `1037`, ...) | `FAILED` | Not completed; description stored |

### Callback verification (what is and is not verified)

Safaricom's callbacks are **not cryptographically signed**, so nobody can prove a
callback came from Safaricom using a signature. This project instead layers these
defences:

1. **Secret URL token.** The callback URL ends in `/api/callback/<CALLBACK_TOKEN>`.
   Requests with a wrong or missing token get `403` and change nothing.
2. **Known payments only.** A callback is applied only if we created that
   `CheckoutRequestID` ourselves in `/api/pay`. Unknown IDs are ignored.
3. **Amount check.** A "success" whose amount differs from what we requested is
   stored as `FAILED` for manual review.
4. **Idempotent writes.** `UPDATE ... WHERE status = 'PENDING'` means duplicate,
   replayed or conflicting callbacks cannot change a finished payment. Receipt
   numbers are `UNIQUE`. (Tested with 25 simultaneous conflicting callbacks on
   real Postgres: exactly one won.)
5. **Direct confirmation.** The STK Query fallback asks Safaricom directly using
   our authenticated session, so a fake callback cannot decide the outcome there.
6. **Optional IP allow-list** (`CALLBACK_ALLOWED_IPS`), off by default. Published
   Safaricom IP ranges are community-documented rather than guaranteed, so treat
   this as an extra layer, never the only one.

The token is a shared secret that also travels in the URL, so it may show up in
host request logs. If you ever think it leaked, rotate it (section 12).

### Data stored per payment

`checkout_request_id` (unique), `merchant_request_id`, `status`, `result_code`,
`result_desc` (max 255 chars), `amount`, `phone_masked` (like `2547*****678`, the full
number is **never** stored or logged), `mpesa_receipt` (unique), `callback_count`,
`created_at`, `updated_at`, `last_query_at`. PINs and passwords are never seen by this app.

**Lifecycle:** created as `PENDING` in `/api/pay` -> updated once by the first
callback (or query) -> read by `/api/payment-status`. The table is created
automatically on first use (`schema.sql` is the same thing for manual use). After a
restart, nothing is lost because data lives in Neon, not on Render.

---

## 2. Final folder layout

```
STK/
├── server.py                 Flask app: routes, Daraja client, callback logic
├── db.py                     Database layer (Postgres on Neon, SQLite locally)
├── check_setup.py            Checks your config without printing secrets
├── check_secrets.sh          Checks no secrets leaked into Git
├── requirements.txt          Pinned runtime dependencies
├── requirements-dev.txt      Adds pytest for testing
├── render.yaml               Optional Render Blueprint (reference)
├── schema.sql                Table definition (optional, app creates it)
├── .python-version           Tells Render which Python to use (3.12)
├── .env.example              Placeholder settings (safe to commit)
├── .gitignore                Keeps secrets, venv, databases out of Git
├── README.md  TEST_PLAN.md  DEPLOYMENT_CHECKLIST.md
├── static/
│   ├── index.html            Payment page
│   ├── style.css             Styles (your original look, plus status states)
│   └── app.js                Validation, polling, result states
└── tests/
    ├── conftest.py  test_app.py          102 backend tests (pytest)
    └── frontend/polling.test.mjs         7 optional browser-logic tests (Node)
```

**What changed from your original ZIP:** the three frontend files moved into
`static/` (URLs `/`, `/style.css`, `/app.js` are unchanged); `/api/auth-test` was
removed because it was public (use `python check_setup.py --daraja` instead); the
callback URL now has a secret token on the end; `/api/payment-status/<id>`, a database,
polling and the new states were added. `/api/test`, `/api/pay` and `/api/callback`
keep their paths.

---

## 3. Part A: Set up on Kali (steps 1-9)

All commands are for a Kali terminal. Lines starting with `#` are comments.

### Step 1. Extract the ZIP into a clean folder

**Directory:** wherever you downloaded `stk-app.zip` (usually `~/Downloads`).

```bash
cd ~/Downloads
sudo apt install -y unzip          # only needed if "unzip: command not found"
unzip stk-app.zip -d ~/Documents/STK-v2
cd ~/Documents/STK-v2
ls -la
```

*What it does:* unzips into a **new** folder so your old `~/Documents/STK` stays untouched.
*Success looks like:* `ls` lists `server.py`, `db.py`, `static`, `tests`, `README.md`, etc.
*If it fails:* "cannot find zipfile" means you are in the wrong folder; `ls` to find the file, or give the full path.

### Step 2. Look at what you have

**Directory:** `~/Documents/STK-v2`

```bash
ls static tests
head -20 server.py
```

*Success:* you see the files from the layout in section 2. Nothing secret is in here; `.env` is deliberately absent.

### Step 3. Install system packages

**Directory:** any.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl
python3 --version
git --version
```

*What it does:* installs Python, virtual environments, Git and curl.
*Success:* versions print (Python 3.11 or newer, tested on 3.12).
*If it fails:* check your internet; if `apt update` complains about keys, run `sudo apt update --allow-releaseinfo-change`.

### Step 4. Create and activate a virtual environment

**Directory:** `~/Documents/STK-v2`

```bash
python3 -m venv venv
source venv/bin/activate
```

*What it does:* makes a private Python workspace (`venv/`) so packages do not touch your system Python (Kali blocks system-wide `pip` on purpose).
*Success:* your prompt starts with `(venv)`.
*Every new terminal:* run `source venv/bin/activate` again from this folder.

### Step 5. Install the Python packages

**Directory:** `~/Documents/STK-v2`, with `(venv)` showing.

```bash
pip install -r requirements-dev.txt
```

*Success:* ends with `Successfully installed Flask-3.1.3 ...`.
*If it fails:* "No matching distribution" usually means no internet or an old Python; check `python --version` is 3.11+.

### Step 6. Create your local settings file

**Directory:** `~/Documents/STK-v2`

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
nano .env
```

The second command prints a random string. In `nano`, fill in these lines:

| Variable | What to put |
|---|---|
| `DARAJA_CONSUMER_KEY`, `DARAJA_CONSUMER_SECRET` | Your **Sandbox** app keys from developer.safaricom.co.ke (copy from your old `.env`) |
| `DARAJA_SHORTCODE`, `DARAJA_PASSKEY` | Sandbox values (copy from your old `.env`) |
| `CALLBACK_TOKEN` | The random string you just generated |
| `DARAJA_CALLBACK_URL` | For now: `https://example.com/api/callback/` followed by the same token (you will fix this after deploying, section 7) |
| `DATABASE_URL` | Leave **empty** for now (uses a local file) |

Save with `Ctrl+O`, `Enter`, exit with `Ctrl+X`.

*Never paste these values into chat, screenshots or GitHub.* `.env` is ignored by Git.

### Step 7. Check your configuration (prints no secrets)

**Directory:** `~/Documents/STK-v2`, `(venv)` active.

```bash
python check_setup.py
```

*Success:* every required variable says `SET`, then `Configuration problems: none`, `Database: reachable`, `RESULT: all good`.
*If it fails:* it names the variable or rule that is wrong, for example "DARAJA_CALLBACK_URL must end with /api/callback/<CALLBACK_TOKEN>". Fix `.env` and run again.

Then test the Daraja Sandbox login:

```bash
python check_setup.py --daraja
```

*Success:* `Daraja Sandbox login: OK`. *If `FAILED (auth_failed)`:* the consumer key/secret are wrong or the app is not a Sandbox app. *If `(unreachable)`:* check internet.

### Step 8. Run the automated tests

**Directory:** `~/Documents/STK-v2`, `(venv)` active.

```bash
pytest -q
```

*What it does:* runs 102 tests with a **fake** Daraja, so nothing real is called and no money or phone is involved.
*Success:* `102 passed`.
*If any fail:* read the first `FAILED` line; do not deploy until it is fixed. These tests never use your `.env` secrets.

Optional (needs Node.js; skip if you do not have it):

```bash
sudo apt install -y nodejs npm
cd tests/frontend && npm install && npm test && cd ../..
```

*Success:* `# pass 7`.

### Step 9. Run the app locally

**Directory:** `~/Documents/STK-v2`, `(venv)` active.

```bash
python server.py
```

*Success:* `Running on http://127.0.0.1:5000`. Leave it running. Open a **second** terminal:

```bash
curl -s http://127.0.0.1:5000/api/test
```

*Success:* `{"configured":true,"message":"Backend is working","success":true}`.
Open http://127.0.0.1:5000 in Firefox, and try a bad phone number like `12345` to see the validation message. Stop the server with `Ctrl+C`.

> Local testing cannot receive Safaricom's callback unless you expose it with a tunnel.
> That is why the real end-to-end test is done on Render (Part D). If you want to
> test locally anyway, start `cloudflared tunnel --url http://127.0.0.1:5000`, put the
> tunnel's `https://....trycloudflare.com/api/callback/<your token>` in `DARAJA_CALLBACK_URL`,
> and restart the app. The URL changes each time the tunnel restarts.

---

## 4. Part B: Create the free database on Neon (steps 10-11)

### Step 10. Create the project

1. Go to **https://console.neon.tech** and sign up (GitHub login is easiest). No card is requested for the Free plan.
2. Click **Create project**. Name it `stk-demo`. For the region choose the AWS region nearest Kenya that is offered, such as **Europe (Frankfurt)**, and remember it: choose the **same region** for Render later.
3. On the project dashboard click **Connect**. Copy the connection string. It looks like
   `postgresql://USER:PASSWORD@ep-xxxx.region.aws.neon.tech/neondb?sslmode=require`.

*Treat this string like a password.*

### Step 11. Test the database from your laptop

**Directory:** `~/Documents/STK-v2`, `(venv)` active.

```bash
nano .env        # paste the Neon string after DATABASE_URL=   then save
python check_setup.py
```

*Success:* `Database: reachable`. The first connection can take a few seconds because Neon wakes from sleep.
*If it fails:* make sure you pasted the whole string with no spaces or quotes, and that it includes `?sslmode=require`.
Run `pytest -q` again if you like; the tests use their own temporary database and never touch Neon.

Afterwards you can blank `DATABASE_URL` again to return to the local SQLite file for development.

---

## 5. Part C: Git and GitHub (steps 12-15)

### Step 12. Create the local Git repository

**Directory:** `~/Documents/STK-v2`

```bash
git init -b main
git config user.name "Your Name"
git config user.email "you@example.com"
git add .
git status
```

*Success:* `git status` lists the project files in green. **It must NOT list** `.env`, `venv/`, `local_dev.db` or `__pycache__`. If it does, stop and check `.gitignore` exists.

### Step 13. Check that no secret is committed

```bash
bash check_secrets.sh
git check-ignore -v .env
```

*Success:* `OK: no values from .env were found...` and a line showing `.gitignore` ignores `.env`.
*If it says PROBLEM:* do **not** push. Remove the file from Git (`git rm --cached <file>`), and if a secret was ever committed, rotate it (section 12).

### Step 14. Commit

```bash
git commit -m "STK Push demo: callback, status tracking, Postgres storage"
```

*Success:* a summary of the files added. This command only affects your local copy.

### Step 15. Create the GitHub repository and push

1. On github.com click **+ -> New repository**. Name it `stk-push-demo`. Choose **Private**. Do **not** tick "Add a README". Click **Create repository**.
2. Create an access token (GitHub no longer accepts account passwords for pushing): **Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens -> Generate new token**. Limit it to this one repository with **Contents: Read and write**. Copy the token once.
3. Back in the terminal:

```bash
git remote add origin https://github.com/YOUR-USERNAME/stk-push-demo.git
git push -u origin main
```

When asked for *Username*, type your GitHub username. When asked for *Password*, paste the **token** (nothing appears on screen as you paste; press Enter).

*Success:* `Branch 'main' set up to track 'origin/main'`, and your files appear on github.com.
*If it fails:* `Authentication failed` means the token lacks access to this repo or expired; make a new one.

---

## 6. Part D: Deploy on Render (steps 16-20)

### Step 16. Create the Render account

Go to **https://render.com**, click **Get Started**, and sign in with GitHub. Free web services should not need a card; if Render asks for one, check you picked the Free instance type, and stop if it is not free for you.

### Step 17. Create the web service

1. **New + -> Web Service -> Build and deploy from a Git repository**. Allow Render access to the `stk-push-demo` repo and select it.
2. Fill in the form (labels can differ slightly):

| Field | Value |
|---|---|
| Name | `stk-push-demo` (your URL will be `https://<name>.onrender.com`; if the name is taken Render adds letters, so read the real URL at the top of the service page) |
| Language | Python 3 |
| Branch | `main` |
| Region | same region as Neon (for example Frankfurt) |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn server:app --workers 1 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT` |
| Instance Type | **Free** |

3. Under **Advanced**, set **Health Check Path** to `/api/test`.

### Step 18. Set the environment variables (private, on the host)

On the same page (or later under **Environment**), click **Add Environment Variable** for each:

| Key | Value |
|---|---|
| `APP_ENV` | `production` |
| `DATABASE_URL` | your Neon connection string |
| `DARAJA_CONSUMER_KEY` | Sandbox key |
| `DARAJA_CONSUMER_SECRET` | Sandbox secret |
| `DARAJA_SHORTCODE` | `174379` (or your Sandbox shortcode) |
| `DARAJA_PASSKEY` | Sandbox passkey |
| `CALLBACK_TOKEN` | the same random token as in your local `.env` |
| `DARAJA_CALLBACK_URL` | `https://<your-service>.onrender.com/api/callback/<CALLBACK_TOKEN>` (best guess for now; you will confirm it in step 19) |
| `MAX_AMOUNT` | `1000` |
| `TRUSTED_PROXY_HOPS` | `1` |

Open your local `.env` in `nano` to copy the values across. They are stored by Render and are never in Git, HTML, JavaScript or browser responses.

Click **Create Web Service**. Render now builds and starts it.

### Step 19. Find your real HTTPS URL and set the callback URL

1. Watch the **Logs** tab until you see `Your service is live`. (If the build fails see Troubleshooting, section 11.)
2. Read your URL at the top of the service page: `https://something.onrender.com`. This is your **hostname**.
3. If it differs from what you typed, go to **Environment**, edit `DARAJA_CALLBACK_URL` so it is exactly:

```
https://<REAL-HOSTNAME>/api/callback/<CALLBACK_TOKEN>
```

   Then **Save, rebuild, and deploy** (or **Save Changes**; Render redeploys).
4. Verify. The URL must start with `https://`, contain your real hostname (not `localhost`, not `trycloudflare.com`, not a placeholder) and end with the same token as `CALLBACK_TOKEN`. The app itself refuses to take payments if the URL does not match the token.

### Step 20. Verify the public deployment from your terminal

**Directory:** any. Replace `HOST` and `TOKEN`.

```bash
HOST=your-service.onrender.com
TOKEN=paste-your-token-here

# 1. Site + HTTPS + health (first request after idle takes ~1 minute: Render is waking up)
curl -s https://$HOST/api/test?deep=1
# 2. A browser-style GET to the callback must be refused (route is POST-only)
curl -s -o /dev/null -w "%{http_code}\n" https://$HOST/api/callback/$TOKEN
# 3. A wrong token must be rejected
curl -s -w " [%{http_code}]\n" -X POST -H 'Content-Type: application/json' -d '{}' https://$HOST/api/callback/wrong
# 4. The right token with junk must be refused as malformed
curl -s -w " [%{http_code}]\n" -X POST -H 'Content-Type: application/json' -d '{}' https://$HOST/api/callback/$TOKEN
# 5. A well-formed callback for a payment we never created is accepted but ignored
curl -s -w " [%{http_code}]\n" -X POST -H 'Content-Type: application/json' \
  -d '{"Body":{"stkCallback":{"CheckoutRequestID":"ws_CO_not_a_real_id","ResultCode":0,"ResultDesc":"x"}}}' \
  https://$HOST/api/callback/$TOKEN
```

*Success:*

1. `{"configured":true,"database":"ok","message":"Backend is working","success":true}`
2. `405`
3. `{"ResultCode":1,"ResultDesc":"Rejected"} [403]`
4. `{"ResultCode":1,"ResultDesc":"Malformed payload"} [400]`
5. `{"ResultCode":0,"ResultDesc":"Accepted"} [200]` (and nothing is stored)

*What this proves, and what it does not:* the route is public over HTTPS, accepts POST, and applies the token and validation rules. It does **not** prove Safaricom can reach you. Only a real Sandbox payment (next part) proves that.
*If `configured` is `false`:* an environment variable is missing or `DARAJA_CALLBACK_URL` does not end with the token. *If `database` is `unavailable`:* check `DATABASE_URL`.

---

## 7. Part E: Test a full Sandbox payment (step 21)

Open `https://<your-host>/` on your phone or laptop (the first load may take about a minute). Use a Safaricom number that is registered for your Daraja Sandbox testing. Use small amounts.

Run these three scenarios. After each, open the Render **Logs** and look for a line like
`Callback: checkout=ws_CO_... code=1032 outcome=updated`.

| Scenario | What you do | The page should show | Stored status |
|---|---|---|---|
| Cancel | Press **Cancel** on the prompt | "Payment cancelled" (amber) | `CANCELLED`, code 1032 |
| Success | Enter your PIN | "Payment successful" with a receipt | `SUCCESS`, code 0 |
| Failure | If you can, enter a wrong PIN or let the prompt time out | "Payment failed" (red) with a reason | `FAILED`, other code |

**Interpreting the results**

* Page shows **pending** for up to ~2 minutes, then "Still waiting for confirmation": no callback and no answer from Daraja's query came back. This is honest, not a success. Click **Check again**. Do not pay twice.
* `outcome=duplicate` in the logs means Safaricom sent the same result again and we correctly ignored it.
* Nothing in the logs at all: Safaricom did not reach the URL. Re-check section 6, step 19, and that the service was awake.

**Look at the database** (Neon console -> SQL Editor):

```sql
SELECT checkout_request_id, status, result_code, amount, phone_masked,
       mpesa_receipt, callback_count, created_at, updated_at
FROM payments ORDER BY id DESC LIMIT 5;
```

**Restart test:** in Render use **Manual Deploy -> Deploy latest commit** (a restart). Reload the page status of an old payment: results must still be there, because they live in Neon.

---

## 8. Reading hosted logs safely

Render dashboard -> your service -> **Logs**. This app logs checkout IDs, result codes, masked phone numbers (`2547*****678`) and error *types*. It does not log credentials, tokens, full phone numbers or tracebacks sent to users. Still, skim a log before sharing it, and never paste your environment variables or connection string anywhere.

---

## 9. Free-tier behaviour you will notice

* **Render free web service sleeps** after 15 minutes without traffic and takes about a minute to wake. While you wait for a PIN prompt the page is polling, which keeps it awake. If a callback ever arrives while asleep, the 30-second Query fallback recovers the result.
* Render gives 750 free instance hours a month per workspace; beyond that the service pauses until next month.
* **Neon free** suspends compute after 5 idle minutes (the next query is a little slower) and has storage and compute-hour limits far above what this demo needs.
* Never use Render's own free Postgres for this project: it expires after 30 days.
* Rate limits (5 payments/minute and 30/hour per visitor) are held in memory, so they reset when the service restarts.

Free tiers change. Re-read the current limits on Render's and Neon's pricing pages before relying on them.

---

## 10. Update and redeploy (step 22)

**Directory:** `~/Documents/STK-v2`, `(venv)` active.

```bash
pytest -q                      # must say: 102 passed
git add -A
bash check_secrets.sh          # must say: OK
git commit -m "Describe your change"
git push
```

Render detects the push and redeploys automatically (watch **Events**/**Logs**). Changes to environment variables in the dashboard also trigger a redeploy.

---

## 11. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Build fails with a Python version error | `.python-version` says `3.12`. Render supports it; if it complains, check the file is in the repo root and committed |
| `ModuleNotFoundError` on start | Build Command is not `pip install -r requirements.txt`, or a package is missing from `requirements.txt` |
| Service starts then "Port scan timeout" | Start Command must use `--bind 0.0.0.0:$PORT` exactly |
| `/api/test` says `"configured":false` | A variable is missing, or `DARAJA_CALLBACK_URL` does not end with `/api/callback/<CALLBACK_TOKEN>` or is not `https://` |
| `"database":"unavailable"` | Wrong `DATABASE_URL`, or Neon is waking up; retry in 10 seconds |
| Page says "Payments are not available right now" (503) | Same as `configured:false` |
| "Could not reach the payment provider" (502) | Wrong consumer key/secret, or Daraja is down. Run `python check_setup.py --daraja` locally |
| Prompt arrives, page stays pending, no callback logged | Wrong hostname/token in `DARAJA_CALLBACK_URL`, or the service restarted; fix and redeploy |
| `429 Too many requests` | You hit the rate limit; wait a minute |
| First page load is very slow | Render free service waking up (normal) |
| `git push` rejected | Token expired or lacks access; make a new fine-grained token |
| Logs show `Callback rejected: bad or missing token` | The URL Safaricom is using has a different token; update `DARAJA_CALLBACK_URL` |

---

## 12. Secrets: rotating and separating

* **Local vs hosted:** local values live in `.env` (ignored by Git). Hosted values live only in the Render dashboard.
* **Sandbox vs production:** this app talks only to `sandbox.safaricom.co.ke` (hard-coded). Live credentials must never be put into this project (see section 14).
* **Verify configuration without printing secrets:** `python check_setup.py`.
* **If a Daraja key may have been exposed** (pasted in chat, committed, screenshot): create a new Sandbox app in the Daraja portal to get fresh keys, update `.env` and the Render variables, delete the old app. If it was committed, also assume it is permanently public, even after deleting the file.
* **Rotate the callback token:** generate a new one (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), then change **both** `CALLBACK_TOKEN` and the end of `DARAJA_CALLBACK_URL` in Render in the same save. In-flight payments sent before the change will have their callbacks rejected; the Query fallback still resolves them.
* **Rotate the Neon password:** in Neon, reset the role's password, copy the new connection string into Render's `DATABASE_URL`.

---

## 13. Security summary

* Server-side validation of phone (`2547`/`2541` numbers) and amount (whole KSh, 1 to `MAX_AMOUNT`).
* No debug mode (`debug=False`; gunicorn in production); errors return generic JSON, tracebacks only in server logs.
* Browser can read a status but cannot write one; only authenticated callbacks and Daraja queries can.
* Status endpoint returns minimal data and `404` for unknown or badly formatted IDs. Daraja checkout IDs are long and unguessable, and lookups are rate-limited.
* Same-origin only: the page and API are served from one host, so **no CORS is enabled** (nothing to misconfigure). If you ever host the page elsewhere, add an explicit origin allow-list.
* Security headers (CSP, `X-Frame-Options`, `nosniff`, HSTS over HTTPS, `no-store` on API responses).
* Rate limiting on `/api/pay` (5/min, 30/hour), `/api/test` (30/min), `/api/payment-status` (60/min). Per-visitor identification relies on `TRUSTED_PROXY_HOPS`; if the limit seems to apply to everyone at once or to nobody, adjust that value.

---

## 14. Limitations (and what production would need)

This project is a **free learning/demo deployment**, not a production payment system.

* Sandbox only. Going live needs a real Paybill/Till, Safaricom's go-live approval, live credentials, and possibly Safaricom whitelisting your server's outbound IPs. Render's free service has no stable outbound IP and sleeps; production needs paid always-on hosting.
* Callbacks are unsigned (see section 1). Production should add IP allow-listing and reconcile every payment with Safaricom.
* No user accounts, orders or receipts; it records payments but does not release goods against them.
* Free Neon has no guaranteed backups; production needs backups and monitoring.
* In-memory rate limiting resets on restart and covers one process only.
* The amount cap (`MAX_AMOUNT`) is a demo safety limit.
* Free tiers can change or end without notice.

---

## 15. Stop and clean up when finished

| Resource | How to remove | Effect |
|---|---|---|
| Render service | Service -> **Settings** -> scroll to **Delete Web Service** | Site goes offline permanently; env vars are deleted with it |
| Neon project | Project -> **Settings** -> **Delete project** | **Deletes all payment records** permanently |
| GitHub repo | Repo -> **Settings** -> **Danger Zone** -> **Delete this repository** | Deletes your code on GitHub (your local copy stays) |
| GitHub token | Settings -> Developer settings -> Tokens -> **Delete** | Token stops working |
| Local virtual env | `deactivate` then `rm -rf venv` (inside `~/Documents/STK-v2` only) | Removes installed packages; recreate with steps 4-5 |
| Sandbox keys | Delete the app in the Daraja portal | Old keys stop working |

Double-check the folder before any `rm -rf`; it deletes immediately with no undo.
