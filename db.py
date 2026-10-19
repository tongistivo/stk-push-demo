"""Payment storage for the STK Push demo.

Production uses Postgres (Neon) through DATABASE_URL. Local development and
the automated tests fall back to a SQLite file, so you can run the app with
no database account at all.

Key rules enforced here:
  * checkout_request_id is UNIQUE, so one payment = one row.
  * A result is written only while the row is still PENDING, so duplicate or
    late callbacks can never change a finished payment (idempotent).
  * mpesa_receipt is UNIQUE, so the same receipt cannot appear twice.
"""

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

log = logging.getLogger("stk.db")

BASE_DIR = Path(__file__).resolve().parent

PENDING, SUCCESS, CANCELLED, FAILED = "PENDING", "SUCCESS", "CANCELLED", "FAILED"

metadata = MetaData()

payments = Table(
    "payments",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("checkout_request_id", String(64), nullable=False, unique=True),
    Column("merchant_request_id", String(64)),
    Column("status", String(16), nullable=False, server_default=PENDING),
    Column("result_code", Integer),
    Column("result_desc", String(255)),
    Column("amount", Integer, nullable=False),
    Column("phone_masked", String(16)),
    Column("mpesa_receipt", String(32), unique=True),
    Column("callback_count", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_query_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('PENDING','SUCCESS','CANCELLED','FAILED')",
        name="ck_payments_status",
    ),
    CheckConstraint("amount > 0", name="ck_payments_amount"),
)


class DatabaseError(Exception):
    """Raised for any storage problem. Never carries connection details."""


_lock = threading.Lock()
_state = {"engine": None, "url": None, "schema_ready": False}


def is_production():
    return os.getenv("APP_ENV", "development").strip().lower() == "production"


def database_configured():
    """True when the app has somewhere safe to store payments."""
    if os.getenv("DATABASE_URL", "").strip():
        return True
    return not is_production()  # SQLite fallback is for development only


def _database_url():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        if is_production():
            raise DatabaseError("DATABASE_URL is not set")
        return "sqlite:///" + str(BASE_DIR / "local_dev.db")
    # Neon and most hosts give postgres:// or postgresql://; we use psycopg 3.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def get_engine():
    url = _database_url()
    with _lock:
        if _state["engine"] is None or _state["url"] != url:
            if _state["engine"] is not None:
                _state["engine"].dispose()
            kwargs = {}
            if url.startswith("postgresql"):
                kwargs = {
                    "pool_pre_ping": True,   # survives Neon waking from sleep
                    "pool_recycle": 240,
                    "pool_size": 3,
                    "max_overflow": 2,
                    "connect_args": {"connect_timeout": 10},
                }
            _state["engine"] = create_engine(url, **kwargs)
            _state["url"] = url
            _state["schema_ready"] = False
        return _state["engine"]


def reset_engine():
    """Drop the cached engine (used by tests to simulate a restart)."""
    with _lock:
        if _state["engine"] is not None:
            _state["engine"].dispose()
        _state.update(engine=None, url=None, schema_ready=False)


def _ready_engine():
    try:
        engine = get_engine()
        if not _state["schema_ready"]:
            metadata.create_all(engine)  # CREATE TABLE IF NOT EXISTS
            _state["schema_ready"] = True
        return engine
    except SQLAlchemyError as exc:
        log.error("Database unavailable: %s", type(exc).__name__)
        raise DatabaseError("database unavailable") from None


def _now():
    return datetime.now(timezone.utc)


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)  # SQLite returns naive times
    return value


def _row_to_dict(row):
    data = dict(row._mapping)
    for key in ("created_at", "updated_at", "last_query_at"):
        data[key] = _aware(data[key])
    return data


def ping():
    try:
        with _ready_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except (SQLAlchemyError, DatabaseError):
        return False


def create_payment(checkout_id, merchant_id, amount, phone_masked):
    """Insert a PENDING payment. Returns False if it already exists."""
    now = _now()
    try:
        with _ready_engine().begin() as conn:
            conn.execute(
                insert(payments).values(
                    checkout_request_id=checkout_id,
                    merchant_request_id=merchant_id,
                    status=PENDING,
                    amount=amount,
                    phone_masked=phone_masked,
                    callback_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        return True
    except IntegrityError:
        return False
    except SQLAlchemyError as exc:
        log.error("create_payment failed: %s", type(exc).__name__)
        raise DatabaseError("could not save payment") from None


def get_payment(checkout_id):
    try:
        with _ready_engine().connect() as conn:
            row = conn.execute(
                select(payments).where(payments.c.checkout_request_id == checkout_id)
            ).first()
        return _row_to_dict(row) if row else None
    except SQLAlchemyError as exc:
        log.error("get_payment failed: %s", type(exc).__name__)
        raise DatabaseError("could not read payment") from None


def record_result(checkout_id, status, result_code, result_desc, receipt=None,
                  count_callback=True):
    """Store a final result exactly once.

    Returns one of:
      "updated"   - this call moved the payment from PENDING to a final state
      "duplicate" - the payment was already final; nothing was changed
      "unknown"   - we never created this checkout ID; nothing was stored
      "conflict"  - the receipt number already belongs to another payment
    """
    now = _now()
    desc = (result_desc or "")[:255]
    try:
        with _ready_engine().begin() as conn:
            seen = conn.execute(
                update(payments)
                .where(payments.c.checkout_request_id == checkout_id)
                .values(
                    callback_count=payments.c.callback_count
                    + (1 if count_callback else 0),
                )
            )
            if seen.rowcount == 0:
                return "unknown"
            changed = conn.execute(
                update(payments)
                .where(
                    and_(
                        payments.c.checkout_request_id == checkout_id,
                        payments.c.status == PENDING,
                    )
                )
                .values(
                    status=status,
                    result_code=result_code,
                    result_desc=desc,
                    mpesa_receipt=receipt,
                    updated_at=now,
                )
            )
            return "updated" if changed.rowcount == 1 else "duplicate"
    except IntegrityError:
        log.error("Receipt number already used by another payment")
        return "conflict"
    except SQLAlchemyError as exc:
        log.error("record_result failed: %s", type(exc).__name__)
        raise DatabaseError("could not save result") from None


def mark_queried(checkout_id):
    try:
        with _ready_engine().begin() as conn:
            conn.execute(
                update(payments)
                .where(payments.c.checkout_request_id == checkout_id)
                .values(last_query_at=_now())
            )
    except SQLAlchemyError as exc:
        log.error("mark_queried failed: %s", type(exc).__name__)
        raise DatabaseError("could not update payment") from None
