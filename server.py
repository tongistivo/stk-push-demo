"""M-PESA STK Push demo (Daraja SANDBOX only).

Routes (unchanged paths from the original project, plus two additions):
  GET  /                              payment page
  GET  /style.css, /app.js            static assets
  GET  /api/test                      health check (no secrets)
  POST /api/pay                       start an STK Push
  POST /api/callback/<CALLBACK_TOKEN> Daraja result callback   (new: token)
  GET  /api/payment-status/<id>       status for the browser   (new)

/api/auth-test was removed because it was public. Use `python check_setup.py
--daraja` from your terminal instead.
"""

import base64
import hmac
import ipaddress
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, request, send_from_directory
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import db

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
load_dotenv(BASE_DIR / ".env")  # real environment variables win over .env

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# The dev server logs full request lines, which would include the callback token.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# Sandbox only. Going live needs Safaricom approval and a different design.
DARAJA_BASE = "https://sandbox.safaricom.co.ke"
AUTH_URL = DARAJA_BASE + "/oauth/v1/generate?grant_type=client_credentials"
STK_URL = DARAJA_BASE + "/mpesa/stkpush/v1/processrequest"
QUERY_URL = DARAJA_BASE + "/mpesa/stkpushquery/v1/query"

EAT = timezone(timedelta(hours=3))  # Kenya has no daylight saving time
CHECKOUT_RE = re.compile(r"[A-Za-z0-9_\-]{8,64}")
REQUIRED_ENV = (
    "DARAJA_CONSUMER_KEY",
    "DARAJA_CONSUMER_SECRET",
    "DARAJA_SHORTCODE",
    "DARAJA_PASSKEY",
    "DARAJA_CALLBACK_URL",
    "CALLBACK_TOKEN",
)
QUERY_AFTER_SECONDS = 30      # ask Daraja directly if no callback by then
QUERY_MIN_INTERVAL = 15       # ...and at most this often per payment

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # callbacks are ~1-2 KB

_hops = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_hops, x_proto=_hops, x_host=_hops)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
    enabled=os.getenv("RATE_LIMIT_ENABLED", "true").lower() != "false",
)


# --------------------------------------------------------------------------
# Configuration helpers (names only are ever reported, never values)
# --------------------------------------------------------------------------
def _env(name):
    return os.getenv(name, "").strip()


def config_problems():
    """Return a list of human-readable problems. Contains no secret values."""
    problems = [f"{n} is not set" for n in REQUIRED_ENV if not _env(n)]
    url, token = _env("DARAJA_CALLBACK_URL"), _env("CALLBACK_TOKEN")
    if url and not url.startswith("https://"):
        problems.append("DARAJA_CALLBACK_URL must start with https://")
    if token and len(token) < 24:
        problems.append("CALLBACK_TOKEN must be at least 24 characters")
    if url and token and not url.endswith("/api/callback/" + token):
        problems.append("DARAJA_CALLBACK_URL must end with /api/callback/<CALLBACK_TOKEN>")
    if not db.database_configured():
        problems.append("DATABASE_URL is not set (required when APP_ENV=production)")
    return problems


def max_amount():
    try:
        return max(1, min(int(os.getenv("MAX_AMOUNT", "1000")), 150000))
    except ValueError:
        return 1000


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------
def normalize_phone(raw):
    """Return 2547XXXXXXXX / 2541XXXXXXXX or None."""
    if not isinstance(raw, str):
        return None
    phone = re.sub(r"[\s\-]", "", raw)
    phone = phone[1:] if phone.startswith("+") else phone
    if phone.startswith("0") and len(phone) == 10:
        phone = "254" + phone[1:]
    return phone if re.fullmatch(r"254[17]\d{8}", phone) else None


def validate_amount(raw):
    """Return (amount, None) or (None, error message)."""
    if isinstance(raw, bool):
        value = None
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str) and re.fullmatch(r"\d{1,9}", raw.strip()):
        value = int(raw.strip())
    else:
        value = None
    if value is None:
        return None, "Amount must be a whole number, e.g. 100"
    if value < 1:
        return None, "Amount must be at least KSh 1"
    if value > max_amount():
        return None, f"Amount cannot be more than KSh {max_amount():,} in this demo"
    return value, None


def mask_phone(phone):
    return phone[:4] + "*" * (len(phone) - 7) + phone[-3:]


# --------------------------------------------------------------------------
# Daraja client
# --------------------------------------------------------------------------
class DarajaError(Exception):
    pass


_token_lock = threading.Lock()
_token = {"value": None, "expires_at": 0.0}


def get_access_token(force=False):
    with _token_lock:
        if not force and _token["value"] and time.time() < _token["expires_at"]:
            return _token["value"]
        try:
            resp = requests.get(
                AUTH_URL,
                auth=(_env("DARAJA_CONSUMER_KEY"), _env("DARAJA_CONSUMER_SECRET")),
                timeout=(5, 15),
            )
        except requests.RequestException as exc:
            raise DarajaError("unreachable") from exc
        if resp.status_code != 200:
            raise DarajaError("auth_failed")
        try:
            data = resp.json()
            value = data["access_token"]
            lifetime = int(data.get("expires_in", 3599))
        except (ValueError, KeyError, TypeError):
            raise DarajaError("bad_auth_response") from None
        _token["value"] = value
        _token["expires_at"] = time.time() + max(lifetime - 60, 30)
        return value


def _stk_password():
    timestamp = datetime.now(EAT).strftime("%Y%m%d%H%M%S")
    raw = _env("DARAJA_SHORTCODE") + _env("DARAJA_PASSKEY") + timestamp
    return timestamp, base64.b64encode(raw.encode()).decode()


def _daraja_post(url, payload_builder):
    """POST with a cached token; retry once if Daraja says the token expired."""
    for attempt in (1, 2):
        token = get_access_token(force=(attempt == 2))
        resp = requests.post(
            url,
            json=payload_builder(),
            headers={"Authorization": f"Bearer {token}"},
            timeout=(5, 25),
        )
        if resp.status_code != 401 or attempt == 2:
            return resp
    return resp  # pragma: no cover


def query_stk_status(checkout_id):
    """Ask Daraja for the result of one STK Push.

    Returns (result_code, description), or None if Daraja has no final answer
    yet (for example "The transaction is being processed").
    """

    def payload():
        timestamp, password = _stk_password()
        return {
            "BusinessShortCode": _env("DARAJA_SHORTCODE"),
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_id,
        }

    resp = _daraja_post(QUERY_URL, payload)
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or "ResultCode" not in data:
        return None
    code = _to_int(data.get("ResultCode"))
    if code is None:
        return None
    return code, str(data.get("ResultDesc", ""))[:255]


# --------------------------------------------------------------------------
# Result interpretation
# --------------------------------------------------------------------------
def _to_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,6}", value.strip()):
        return int(value.strip())
    return None


def status_for_code(code):
    if code == 0:
        return db.SUCCESS
    if code == 1032:
        return db.CANCELLED
    return db.FAILED


FAILURE_TEXT = {
    1: "Your M-Pesa balance is too low for this payment.",
    1001: "Another M-Pesa transaction is already in progress on this number.",
    1019: "The payment request expired.",
    1037: "The request timed out before you responded.",
    2001: "The wrong M-Pesa PIN was entered.",
}


def public_message(row):
    status = row["status"]
    if status == db.PENDING:
        return "Waiting for you to enter your M-Pesa PIN on your phone."
    if status == db.SUCCESS:
        return "Payment received. Thank you."
    if status == db.CANCELLED:
        return "You cancelled the payment request."
    text = FAILURE_TEXT.get(row["result_code"], "The payment was not completed.")
    desc = (row["result_desc"] or "").strip()
    if row["result_code"] not in FAILURE_TEXT and desc:
        text += " Reason: " + desc[:120]
    return text


def parse_callback(data):
    """Validate a Daraja STK callback. Raises ValueError if malformed."""
    body = data.get("Body") if isinstance(data, dict) else None
    cb = body.get("stkCallback") if isinstance(body, dict) else None
    if not isinstance(cb, dict):
        raise ValueError("missing stkCallback")
    checkout_id = cb.get("CheckoutRequestID")
    if not isinstance(checkout_id, str) or not CHECKOUT_RE.fullmatch(checkout_id):
        raise ValueError("bad CheckoutRequestID")
    code = _to_int(cb.get("ResultCode"))
    if code is None:
        raise ValueError("bad ResultCode")
    desc = cb.get("ResultDesc")
    desc = desc[:255] if isinstance(desc, str) else ""

    receipt = amount = None
    meta = cb.get("CallbackMetadata")
    items = meta.get("Item") if isinstance(meta, dict) else None
    if code == 0 and isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("Name") == "MpesaReceiptNumber" and isinstance(item.get("Value"), str):
                if re.fullmatch(r"[A-Za-z0-9]{6,20}", item["Value"]):
                    receipt = item["Value"]
            elif item.get("Name") == "Amount" and isinstance(item.get("Value"), (int, float)):
                amount = float(item["Value"])
    return {
        "checkout_id": checkout_id,
        "code": code,
        "desc": desc,
        "receipt": receipt,
        "amount": amount,
    }


def apply_result(checkout_id, code, desc, receipt=None, amount=None, from_callback=True):
    """Apply a Daraja result to our own record. Returns the db outcome string."""
    status = status_for_code(code)
    if status == db.SUCCESS and amount is not None:
        row = db.get_payment(checkout_id)
        if row is not None and float(row["amount"]) != amount:
            app.logger.warning("Amount mismatch for %s; not marking SUCCESS", checkout_id)
            status = db.FAILED
            desc = "Amount did not match the request. Needs manual review."
            receipt = None
    return db.record_result(
        checkout_id, status, code, desc, receipt, count_callback=from_callback
    )


def maybe_reconcile(row):
    """If a callback is overdue, ask Daraja directly. Never raises."""
    try:
        now = datetime.now(timezone.utc)
        age = (now - row["created_at"]).total_seconds()
        last = row["last_query_at"]
        if age < QUERY_AFTER_SECONDS:
            return row
        if last is not None and (now - last).total_seconds() < QUERY_MIN_INTERVAL:
            return row
        db.mark_queried(row["checkout_request_id"])
        answer = query_stk_status(row["checkout_request_id"])
        if answer is None:
            return row
        code, desc = answer
        apply_result(row["checkout_request_id"], code, desc, from_callback=False)
        return db.get_payment(row["checkout_request_id"]) or row
    except Exception as exc:  # reconciliation must never break the status call
        app.logger.warning("Reconciliation skipped: %s", type(exc).__name__)
        return row


# --------------------------------------------------------------------------
# Static pages
# --------------------------------------------------------------------------
@app.route("/")
def home():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/style.css")
def css():
    return send_from_directory(STATIC_DIR, "style.css")


@app.route("/app.js")
def javascript():
    return send_from_directory(STATIC_DIR, "app.js")


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.route("/api/test")
@limiter.limit("30 per minute")
def health():
    body = {
        "success": True,
        "message": "Backend is working",
        "configured": not config_problems(),
    }
    if request.args.get("deep") == "1":  # also wakes the database
        body["database"] = "ok" if db.ping() else "unavailable"
    return body


@app.route("/api/pay", methods=["POST"])
@limiter.limit("5 per minute;30 per hour")
def pay():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"success": False, "message": "Send the details as a JSON object"}, 400

    phone = normalize_phone(data.get("phone"))
    if phone is None:
        return {
            "success": False,
            "message": "Enter a valid Safaricom number, e.g. 0712345678",
        }, 400
    amount, error = validate_amount(data.get("amount"))
    if error:
        return {"success": False, "message": error}, 400

    if config_problems():
        app.logger.error("Payment blocked: configuration incomplete: %s", config_problems())
        return {
            "success": False,
            "message": "Payments are not available right now. Please try again later.",
        }, 503

    def payload():
        timestamp, password = _stk_password()
        shortcode = _env("DARAJA_SHORTCODE")
        return {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": amount,
            "PartyA": phone,
            "PartyB": shortcode,
            "PhoneNumber": phone,
            "CallBackURL": _env("DARAJA_CALLBACK_URL"),
            "AccountReference": "STKDEMO",
            "TransactionDesc": "Test payment",
        }

    try:
        resp = _daraja_post(STK_URL, payload)
        result = resp.json()
    except DarajaError:
        app.logger.error("Could not authenticate with Daraja")
        return {"success": False, "message": "Could not reach the payment provider. Try again."}, 502
    except requests.Timeout:
        app.logger.error("Daraja timed out")
        return {"success": False, "message": "The payment provider took too long. Try again."}, 504
    except requests.RequestException:
        app.logger.error("Network error talking to Daraja")
        return {"success": False, "message": "Could not reach the payment provider. Try again."}, 502
    except ValueError:
        app.logger.error("Daraja returned a non-JSON response")
        return {"success": False, "message": "The payment provider sent an unexpected reply."}, 502

    checkout_id = result.get("CheckoutRequestID") if isinstance(result, dict) else None
    accepted = (
        resp.ok
        and isinstance(result, dict)
        and str(result.get("ResponseCode")) == "0"
        and isinstance(checkout_id, str)
        and CHECKOUT_RE.fullmatch(checkout_id)
    )
    if not accepted:
        app.logger.warning("STK request rejected (HTTP %s)", resp.status_code)
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("errorMessage") or result.get("ResponseDescription") or "")
        return {
            "success": False,
            "message": reason[:150] or "The payment provider rejected the request.",
        }, 502

    merchant_id = result.get("MerchantRequestID")
    merchant_id = merchant_id[:64] if isinstance(merchant_id, str) else None
    try:
        db.create_payment(checkout_id, merchant_id, amount, mask_phone(phone))
    except db.DatabaseError:
        app.logger.error("STK sent but payment %s could not be stored", checkout_id)
        return {
            "success": False,
            "message": (
                "The request was sent but could not be tracked. "
                "Do not pay again; check your M-Pesa messages."
            ),
        }, 503

    app.logger.info("STK accepted: checkout=%s phone=%s amount=%s",
                    checkout_id, mask_phone(phone), amount)
    return {
        "success": True,
        "message": "Check your phone and enter your M-Pesa PIN.",
        "checkoutRequestId": checkout_id,
        "merchantRequestId": merchant_id,
    }


def _callback_ip_allowed():
    allowed = _env("CALLBACK_ALLOWED_IPS")
    if not allowed:
        return True  # optional extra layer; off by default
    try:
        remote = ipaddress.ip_address(request.remote_addr or "")
        return any(remote in ipaddress.ip_network(c.strip(), strict=False)
                   for c in allowed.split(",") if c.strip())
    except ValueError:
        return False


@app.route("/api/callback", defaults={"token": None}, methods=["POST"])
@app.route("/api/callback/<token>", methods=["POST"])
def mpesa_callback(token):
    expected = _env("CALLBACK_TOKEN")
    if not expected or token is None or not hmac.compare_digest(
        token.encode(), expected.encode()
    ):
        app.logger.warning("Callback rejected: bad or missing token")
        return {"ResultCode": 1, "ResultDesc": "Rejected"}, 403
    if not _callback_ip_allowed():
        app.logger.warning("Callback rejected: source address not allowed")
        return {"ResultCode": 1, "ResultDesc": "Rejected"}, 403

    try:
        parsed = parse_callback(request.get_json(silent=True, force=True))
    except ValueError as exc:
        app.logger.warning("Malformed callback ignored: %s", exc)
        return {"ResultCode": 1, "ResultDesc": "Malformed payload"}, 400

    try:
        outcome = apply_result(
            parsed["checkout_id"], parsed["code"], parsed["desc"],
            parsed["receipt"], parsed["amount"],
        )
    except db.DatabaseError:
        # 500 lets Daraja retry; our record is still PENDING so nothing is lost.
        return {"ResultCode": 1, "ResultDesc": "Temporary error"}, 500

    app.logger.info("Callback: checkout=%s code=%s outcome=%s",
                    parsed["checkout_id"], parsed["code"], outcome)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}, 200


@app.route("/api/payment-status/<checkout_id>")
@limiter.limit("60 per minute")
def payment_status(checkout_id):
    unknown = ({"success": False, "status": "UNKNOWN", "message": "Payment not found."}, 404)
    if not CHECKOUT_RE.fullmatch(checkout_id):
        return unknown
    try:
        row = db.get_payment(checkout_id)
        if row is None:
            return unknown
        if row["status"] == db.PENDING:
            row = maybe_reconcile(row)
    except db.DatabaseError:
        return {"success": False, "status": "ERROR",
                "message": "Status is temporarily unavailable."}, 503
    body = {
        "success": True,
        "status": row["status"],
        "message": public_message(row),
        "resultCode": row["result_code"],
        "updatedAt": row["updated_at"].isoformat(),
    }
    if row["status"] == db.SUCCESS and row["mpesa_receipt"]:
        body["receipt"] = row["mpesa_receipt"]
    return body


# --------------------------------------------------------------------------
# Safety nets
# --------------------------------------------------------------------------
@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(HTTPException)
def http_error(exc):
    messages = {
        404: "Not found.",
        405: "Method not allowed.",
        413: "Request too large.",
        429: "Too many requests. Please wait a moment and try again.",
    }
    return {"success": False, "message": messages.get(exc.code, exc.name)}, exc.code


@app.errorhandler(Exception)
def unhandled(exc):
    app.logger.exception("Unhandled error")  # details go to the server log only
    return {"success": False, "message": "Something went wrong on our side."}, 500


if __name__ == "__main__":
    # Local development only. On Render, gunicorn starts the app (see render.yaml).
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
