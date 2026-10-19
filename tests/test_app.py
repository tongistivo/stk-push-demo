import logging
from datetime import datetime, timedelta, timezone

import pytest
import requests
from sqlalchemy import func, select, update

import db
import server
from conftest import CHECKOUT, PHONE, SECRETS, TOKEN, Resp, callback_body, post_callback, start_payment


def status_of(client, checkout=CHECKOUT):
    r = client.get(f"/api/payment-status/{checkout}")
    return r.status_code, r.get_json()


def age_payment(seconds, checkout=CHECKOUT, queried=None):
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    with db.get_engine().begin() as conn:
        conn.execute(update(db.payments)
                     .where(db.payments.c.checkout_request_id == checkout)
                     .values(created_at=past, last_query_at=queried))


# ---- 1-2. Homepage, assets, health ---------------------------------------
def test_homepage_and_static_assets(client):
    assert b"M-Pesa Payment" in client.get("/").data
    assert client.get("/style.css").status_code == 200
    assert client.get("/app.js").status_code == 200


def test_source_and_secret_files_are_not_served(client):
    for path in ("/server.py", "/db.py", "/.env", "/render.yaml", "/static/app.js"):
        assert client.get(path).status_code == 404


def test_removed_debug_route_is_gone(client):
    assert client.get("/api/auth-test").status_code == 404


def test_health_check_has_no_secrets(client):
    r = client.get("/api/test")
    assert r.status_code == 200 and r.get_json()["success"] is True
    assert r.get_json()["configured"] is True
    for value in [*SECRETS.values(), TOKEN]:
        assert value not in r.get_data(as_text=True)


def test_health_deep_checks_database(client):
    assert client.get("/api/test?deep=1").get_json()["database"] == "ok"


# ---- 3. Missing configuration --------------------------------------------
@pytest.mark.parametrize("name", server.REQUIRED_ENV)
def test_missing_env_blocks_payments_safely(client, fake, monkeypatch, name):
    monkeypatch.delenv(name)
    assert client.get("/api/test").get_json()["configured"] is False
    r = client.post("/api/pay", json={"amount": 10, "phone": PHONE})
    assert r.status_code == 503
    assert fake.stk_calls == 0
    assert name not in r.get_data(as_text=True)


def test_callback_url_must_match_token(client, monkeypatch):
    monkeypatch.setenv("DARAJA_CALLBACK_URL", "https://demo.example.com/api/callback")
    assert any("must end with" in p for p in server.config_problems())
    monkeypatch.setenv("DARAJA_CALLBACK_URL", f"http://demo.example.com/api/callback/{TOKEN}")
    assert any("https" in p for p in server.config_problems())


def test_production_requires_database_url(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DATABASE_URL")
    assert any("DATABASE_URL" in p for p in server.config_problems())


# ---- 4-5. Validation ------------------------------------------------------
@pytest.mark.parametrize("phone", [
    "", "abc", "12345", "0612345678", "071234567", "07123456789", "254612345678",
    "+1555123456", None, 712345678, ["0712345678"],
])
def test_invalid_phone_rejected(client, fake, phone):
    r = client.post("/api/pay", json={"amount": 10, "phone": phone})
    assert r.status_code == 400 and fake.stk_calls == 0


@pytest.mark.parametrize("amount", [0, -5, 1.5, "abc", "", None, True, 10**9, "1e3", [10]])
def test_invalid_amount_rejected(client, fake, amount):
    r = client.post("/api/pay", json={"amount": amount, "phone": PHONE})
    assert r.status_code == 400 and fake.stk_calls == 0


def test_max_amount_is_configurable(client, fake, monkeypatch):
    assert client.post("/api/pay", json={"amount": 1000, "phone": PHONE}).status_code == 200
    monkeypatch.setenv("MAX_AMOUNT", "50")
    assert client.post("/api/pay", json={"amount": 51, "phone": PHONE}).status_code == 400


@pytest.mark.parametrize("raw", ["0712345678", "+254712345678", "254712345678",
                                 "0712 345 678", "0112345678"])
def test_phone_formats_normalised(client, fake, raw):
    start_payment(client, phone=raw)
    sent = fake.stk_payloads[-1]["PartyA"]
    assert sent.startswith("254") and len(sent) == 12


@pytest.mark.parametrize("body", [None, [], "text", 5])
def test_pay_rejects_non_object_json(client, fake, body):
    assert client.post("/api/pay", json=body).status_code == 400


# ---- 6-7. Daraja auth and STK Push (mocked) -------------------------------
def test_access_token_is_cached(client, fake):
    start_payment(client)
    with db.get_engine().begin() as conn:
        conn.execute(db.payments.delete())
    start_payment(client)
    assert fake.auth_calls == 1 and fake.stk_calls == 2


def test_stk_payload_is_well_formed(client, fake):
    start_payment(client, amount=25)
    p = fake.stk_payloads[0]
    assert p["Amount"] == 25 and p["PartyB"] == "174379"
    assert p["CallBackURL"].endswith("/api/callback/" + TOKEN)
    assert p["TransactionType"] == "CustomerPayBillOnline"
    assert len(p["Timestamp"]) == 14 and p["Password"]


def test_accepted_request_creates_pending_row(client, fake):
    start_payment(client)
    row = db.get_payment(CHECKOUT)
    assert row["status"] == "PENDING" and row["amount"] == 10
    assert row["phone_masked"] == "2547*****678"          # full number never stored
    assert status_of(client)[1]["status"] == "PENDING"


def test_rejected_request_creates_no_row(client, fake):
    fake.stk = Resp(200, {"ResponseCode": "1", "ResponseDescription": "Rejected"})
    r = client.post("/api/pay", json={"amount": 10, "phone": PHONE})
    assert r.status_code == 502 and db.get_payment(CHECKOUT) is None


def test_auth_failure_is_reported_safely(client, fake):
    fake.auth = Resp(401, {"errorMessage": "Bad credentials"})
    r = client.post("/api/pay", json={"amount": 10, "phone": PHONE})
    assert r.status_code == 502 and "credentials" not in r.get_data(as_text=True).lower()


# ---- 16. Timeouts and provider errors ------------------------------------
@pytest.mark.parametrize("where,exc,code", [
    ("auth", requests.Timeout(), 502),
    ("stk", requests.Timeout(), 504),
    ("stk", requests.ConnectionError(), 502),
])
def test_network_errors(client, fake, where, exc, code):
    fake.raises[where] = exc
    r = client.post("/api/pay", json={"amount": 10, "phone": PHONE})
    assert r.status_code == code and "Traceback" not in r.get_data(as_text=True)


def test_non_json_provider_reply(client, fake):
    fake.stk = Resp(502, None, json_ok=False)
    assert client.post("/api/pay", json={"amount": 10, "phone": PHONE}).status_code == 502


def test_expired_token_is_refreshed_once(client, fake, monkeypatch):
    replies = [Resp(401, {"errorMessage": "Invalid Access Token"}), fake.stk]
    monkeypatch.setattr(server.requests, "post", lambda url, **kw: replies.pop(0))
    start_payment(client)
    assert fake.auth_calls == 2 and not replies


# ---- 8-10. Callback outcomes ---------------------------------------------
def test_success_callback(client, fake):
    start_payment(client)
    assert post_callback(client, callback_body(0)).status_code == 200
    code, body = status_of(client)
    assert body["status"] == "SUCCESS" and body["receipt"] == "NLJ7RT61SV"


def test_cancel_callback_is_distinct(client, fake):
    start_payment(client)
    post_callback(client, callback_body(1032, desc="Request Cancelled by user."))
    body = status_of(client)[1]
    assert body["status"] == "CANCELLED" and "receipt" not in body


@pytest.mark.parametrize("code,text", [(1, "balance"), (2001, "PIN"), (1037, "timed out"), (9999, "not completed")])
def test_other_failures_are_failed(client, fake, code, text):
    start_payment(client)
    post_callback(client, callback_body(code, desc="Some description"))
    body = status_of(client)[1]
    assert body["status"] == "FAILED" and text in body["message"]


def test_success_with_wrong_amount_is_not_trusted(client, fake):
    start_payment(client, amount=10)
    post_callback(client, callback_body(0, amount=1))
    row = db.get_payment(CHECKOUT)
    assert row["status"] == "FAILED" and row["mpesa_receipt"] is None


# ---- 11. Malformed callbacks ---------------------------------------------
@pytest.mark.parametrize("body", [
    None, "garbage", [], {}, {"Body": "x"}, {"Body": {}}, {"Body": {"stkCallback": "x"}},
    {"Body": {"stkCallback": {}}},
    {"Body": {"stkCallback": {"CheckoutRequestID": "../../etc", "ResultCode": 0}}},
    {"Body": {"stkCallback": {"CheckoutRequestID": CHECKOUT, "ResultCode": "abc"}}},
    {"Body": {"stkCallback": {"CheckoutRequestID": CHECKOUT, "ResultCode": True}}},
])
def test_malformed_callbacks_are_rejected_without_crashing(client, fake, body):
    start_payment(client)
    r = post_callback(client, body)
    assert r.status_code == 400
    assert db.get_payment(CHECKOUT)["status"] == "PENDING"


def test_non_json_callback_body(client, fake):
    start_payment(client)
    r = client.post(f"/api/callback/{TOKEN}", data="not json", content_type="text/plain")
    assert r.status_code == 400


# ---- 12. Duplicates / replay ---------------------------------------------
def test_duplicate_callbacks_are_idempotent(client, fake):
    start_payment(client)
    post_callback(client, callback_body(0))
    assert post_callback(client, callback_body(0)).status_code == 200
    row = db.get_payment(CHECKOUT)
    assert row["status"] == "SUCCESS" and row["callback_count"] == 2
    with db.get_engine().connect() as conn:
        assert conn.execute(select(func.count()).select_from(db.payments)).scalar() == 1


def test_late_conflicting_callback_cannot_change_result(client, fake):
    start_payment(client)
    post_callback(client, callback_body(0))
    finished_at = db.get_payment(CHECKOUT)["updated_at"]
    post_callback(client, callback_body(1032))
    assert db.get_payment(CHECKOUT)["status"] == "SUCCESS"
    assert db.record_result(CHECKOUT, "FAILED", 1, "x") == "duplicate"
    row = db.get_payment(CHECKOUT)
    assert row["status"] == "SUCCESS" and row["updated_at"] == finished_at


def test_same_checkout_cannot_be_inserted_twice(client, fake):
    assert db.create_payment(CHECKOUT, "m", 10, "2547*****678") is True
    assert db.create_payment(CHECKOUT, "m", 10, "2547*****678") is False


# ---- Callback authenticity ------------------------------------------------
def test_wrong_token_is_rejected(client, fake):
    start_payment(client)
    r = post_callback(client, callback_body(0), token="wrong-token")
    assert r.status_code == 403 and db.get_payment(CHECKOUT)["status"] == "PENDING"


def test_missing_token_is_rejected(client, fake):
    start_payment(client)
    r = client.post("/api/callback", json=callback_body(0))
    assert r.status_code == 403 and db.get_payment(CHECKOUT)["status"] == "PENDING"


def test_callback_get_is_not_allowed(client):
    assert client.get(f"/api/callback/{TOKEN}").status_code == 405


def test_callback_ip_allowlist(client, fake, monkeypatch):
    start_payment(client)
    monkeypatch.setenv("CALLBACK_ALLOWED_IPS", "196.201.214.0/24")
    r = client.post(f"/api/callback/{TOKEN}", json=callback_body(0),
                    environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
    assert r.status_code == 403
    r = client.post(f"/api/callback/{TOKEN}", json=callback_body(0),
                    environ_overrides={"REMOTE_ADDR": "196.201.214.5"})
    assert r.status_code == 200 and db.get_payment(CHECKOUT)["status"] == "SUCCESS"


# ---- 13-15. Unknown / pending / status changes ----------------------------
def test_unknown_checkout_status(client):
    code, body = status_of(client, "ws_CO_doesnotexist123")
    assert code == 404 and body["status"] == "UNKNOWN"


@pytest.mark.parametrize("bad", ["short", "a b c d e f g h", "x" * 100, "ws_CO_%27OR1=1"])
def test_bad_checkout_ids_are_unknown(client, bad):
    assert client.get(f"/api/payment-status/{bad}").status_code == 404


def test_callback_for_unknown_checkout_is_ignored(client, fake):
    r = post_callback(client, callback_body(0, checkout="ws_CO_never_created_1"))
    assert r.status_code == 200
    assert db.get_payment("ws_CO_never_created_1") is None
    assert db.record_result("ws_CO_never_created_1", "SUCCESS", 0, "x") == "unknown"
    assert status_of(client, "ws_CO_never_created_1")[0] == 404


def test_status_changes_after_callback(client, fake):
    start_payment(client)
    assert status_of(client)[1]["status"] == "PENDING"
    post_callback(client, callback_body(1032))
    assert status_of(client)[1]["status"] == "CANCELLED"


def test_browser_cannot_set_status(client, fake):
    start_payment(client)
    assert client.post(f"/api/payment-status/{CHECKOUT}", json={"status": "SUCCESS"}).status_code == 405
    assert client.put(f"/api/payment-status/{CHECKOUT}").status_code == 405
    assert status_of(client)[1]["status"] == "PENDING"


def test_missing_callback_is_never_success(client, fake):
    start_payment(client)
    age_payment(3600)                       # an hour old, still no answer
    assert status_of(client)[1]["status"] == "PENDING"


# ---- Reconciliation via STK Push Query ------------------------------------
def test_query_fallback_finds_result(client, fake):
    start_payment(client)
    age_payment(60)
    fake.query = Resp(200, {"ResponseCode": "0", "ResultCode": "1032",
                            "ResultDesc": "Request cancelled by user"})
    assert status_of(client)[1]["status"] == "CANCELLED"
    assert fake.query_calls == 1


def test_query_not_used_too_early_or_too_often(client, fake):
    start_payment(client)
    status_of(client)
    assert fake.query_calls == 0                       # younger than 30 s
    age_payment(60)
    status_of(client)
    status_of(client)
    assert fake.query_calls == 1                       # throttled to 1 per 15 s


def test_query_still_processing_stays_pending(client, fake):
    start_payment(client)
    age_payment(60)
    assert status_of(client)[1]["status"] == "PENDING"


def test_query_failure_does_not_break_status(client, fake):
    start_payment(client)
    age_payment(60)
    fake.raises["query"] = requests.Timeout()
    code, body = status_of(client)
    assert code == 200 and body["status"] == "PENDING"


# ---- 17. Persistence across restarts --------------------------------------
def test_status_survives_restart(client, fake):
    start_payment(client)
    db.reset_engine()                                  # simulate process restart
    fresh = server.app.test_client()
    assert status_of(fresh)[1]["status"] == "PENDING"
    post_callback(fresh, callback_body(0))
    db.reset_engine()
    assert status_of(server.app.test_client())[1]["status"] == "SUCCESS"


def test_database_outage_is_reported_safely(client, fake, monkeypatch):
    start_payment(client)

    def boom(_):
        raise db.DatabaseError("down")

    monkeypatch.setattr(db, "get_payment", boom)
    code, body = status_of(client)
    assert code == 503 and body["status"] == "ERROR"


# ---- 18. Production behaviour and safe errors -----------------------------
def test_debug_is_off(client):
    assert server.app.debug is False


def test_unexpected_exception_hides_details(client, monkeypatch):
    def boom(_):
        raise RuntimeError("SECRET-INTERNAL-DETAIL")

    monkeypatch.setattr(db, "get_payment", boom)
    r = client.get(f"/api/payment-status/{CHECKOUT}")
    text = r.get_data(as_text=True)
    assert r.status_code == 500
    assert "SECRET-INTERNAL-DETAIL" not in text and "Traceback" not in text


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert client.get("/api/test").headers["Cache-Control"] == "no-store"


def test_oversized_body_rejected(client):
    assert client.post("/api/pay", data="x" * 20000, content_type="application/json").status_code == 413


def test_rate_limit_on_pay(client, fake):
    server.limiter.enabled = True
    server.limiter.reset()
    try:
        codes = [client.post("/api/pay", json={"amount": 0, "phone": PHONE}).status_code for _ in range(7)]
        assert codes[:5] == [400] * 5 and codes[5] == 429
    finally:
        server.limiter.enabled = False
        server.limiter.reset()


def test_secrets_never_appear_in_logs_or_responses(client, fake, caplog):
    caplog.set_level(logging.DEBUG)
    start_payment(client)
    post_callback(client, callback_body(0))
    post_callback(client, callback_body(0), token="wrong")
    status_of(client)
    text = caplog.text
    for secret in [*SECRETS.values(), TOKEN, "254712345678", PHONE]:
        assert secret not in text


def test_frontend_files_contain_no_secrets():
    from pathlib import Path
    for f in Path(server.STATIC_DIR).iterdir():
        text = f.read_text().lower()
        for word in ("consumer_key", "consumer_secret", "passkey", "callback_token", "bearer"):
            assert word not in text, (f.name, word)
