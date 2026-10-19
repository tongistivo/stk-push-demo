import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import server  # noqa: E402

TOKEN = "t0ken-" + "x" * 30
SECRETS = {
    "DARAJA_CONSUMER_KEY": "test-consumer-key-VALUE",
    "DARAJA_CONSUMER_SECRET": "test-consumer-secret-VALUE",
    "DARAJA_PASSKEY": "test-passkey-VALUE",
}
CHECKOUT = "ws_CO_191220191020363925"
PHONE = "0712345678"


class Resp:
    def __init__(self, status=200, body=None, json_ok=True):
        self.status_code = status
        self.ok = status < 400
        self._body, self._json_ok = body, json_ok

    def json(self):
        if not self._json_ok:
            raise ValueError("not json")
        return self._body


class FakeDaraja:
    """Stands in for Safaricom. Records what the app sent."""

    def __init__(self):
        self.auth = Resp(200, {"access_token": "tok-abc", "expires_in": "3599"})
        self.stk = Resp(200, {
            "MerchantRequestID": "29115-34620561-1",
            "CheckoutRequestID": CHECKOUT,
            "ResponseCode": "0",
            "ResponseDescription": "Success. Request accepted for processing",
        })
        self.query = Resp(500, {"errorCode": "500.001.1001",
                                "errorMessage": "The transaction is being processed"})
        self.raises = {}
        self.auth_calls = self.stk_calls = self.query_calls = 0
        self.stk_payloads = []

    def get(self, url, **kw):
        self.auth_calls += 1
        if "auth" in self.raises:
            raise self.raises["auth"]
        return self.auth

    def post(self, url, json=None, headers=None, **kw):
        key = "query" if "stkpushquery" in url else "stk"
        if key == "query":
            self.query_calls += 1
        else:
            self.stk_calls += 1
            self.stk_payloads.append(json)
        if key in self.raises:
            raise self.raises[key]
        return getattr(self, key)


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DARAJA_SHORTCODE", "174379")
    monkeypatch.setenv("CALLBACK_TOKEN", TOKEN)
    monkeypatch.setenv("DARAJA_CALLBACK_URL", f"https://demo.example.com/api/callback/{TOKEN}")
    # Set TEST_DATABASE_URL to run the same tests against a real Postgres.
    test_url = os.getenv("TEST_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", test_url or f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("CALLBACK_ALLOWED_IPS", raising=False)
    monkeypatch.delenv("MAX_AMOUNT", raising=False)
    server.limiter.enabled = False
    server._token.update(value=None, expires_at=0.0)
    db.reset_engine()
    if test_url:  # start every test with an empty table
        db.metadata.drop_all(db.get_engine())
        db.reset_engine()
    yield
    db.reset_engine()


@pytest.fixture
def fake(env, monkeypatch):
    f = FakeDaraja()
    monkeypatch.setattr(server.requests, "get", f.get)
    monkeypatch.setattr(server.requests, "post", f.post)
    return f


@pytest.fixture
def client(env):
    return server.app.test_client()


def callback_body(code=0, checkout=CHECKOUT, amount=10, receipt="NLJ7RT61SV",
                  desc="The service request is processed successfully."):
    cb = {"MerchantRequestID": "29115-34620561-1", "CheckoutRequestID": checkout,
          "ResultCode": code, "ResultDesc": desc}
    if code == 0:
        cb["CallbackMetadata"] = {"Item": [
            {"Name": "Amount", "Value": amount},
            {"Name": "MpesaReceiptNumber", "Value": receipt},
            {"Name": "PhoneNumber", "Value": 254712345678},
        ]}
    return {"Body": {"stkCallback": cb}}


def post_callback(client, body, token=TOKEN):
    return client.post(f"/api/callback/{token}", json=body)


def start_payment(client, amount=10, phone=PHONE):
    r = client.post("/api/pay", json={"amount": amount, "phone": phone})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["checkoutRequestId"]
