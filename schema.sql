-- Reference schema (Postgres). The app creates this table automatically on
-- first use; you only need this file if you want to create it by hand in the
-- Neon SQL Editor. Running it twice is safe.

CREATE TABLE IF NOT EXISTS payments (
    id                  SERIAL PRIMARY KEY,
    checkout_request_id VARCHAR(64)  NOT NULL UNIQUE,
    merchant_request_id VARCHAR(64),
    status              VARCHAR(16)  NOT NULL DEFAULT 'PENDING',
    result_code         INTEGER,
    result_desc         VARCHAR(255),
    amount              INTEGER      NOT NULL,
    phone_masked        VARCHAR(16),
    mpesa_receipt       VARCHAR(32)  UNIQUE,
    callback_count      INTEGER      NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ  NOT NULL,
    updated_at          TIMESTAMPTZ  NOT NULL,
    last_query_at       TIMESTAMPTZ,
    CONSTRAINT ck_payments_status CHECK (status IN ('PENDING','SUCCESS','CANCELLED','FAILED')),
    CONSTRAINT ck_payments_amount CHECK (amount > 0)
);
