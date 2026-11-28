"""Payments, their state changes and the Inbox of Provider Events (SPEC §4.1, §4.2, ADR 0010).

All in the `payments` schema, and append-only like the Ledger:

- `payments`: one row per Payment the Ledger asked a Provider to make, with its Idempotency
  Key, the Provider's own ID for it and the Customer Balance it funds.
- `inbox`: every verified Provider Event exactly as received (raw body bytes and headers),
  unique by (provider, event_id), so a redelivery is recognised before it is applied.
- `inbox_outcomes`: what applying an Inbox event did, at most once per event: `applied`,
  `stale` (a state already passed: recorded, nothing done) or `exception` (an impossible
  transition: recorded with its reason, nothing written to the Ledger).
- `transitions`: the Payment's state changes, appended, never updated; at most one per
  (Payment, state). Its Actor is the Inbox event that caused it or a system job, and it
  names the Ledger Transaction or Hold the step booked.

The app role gets USAGE on the schema and SELECT and INSERT on these tables, granted here
explicitly (no default privileges, ADR 0008), and each table gets an append-only trigger.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "railbench_app"
TABLES = ("payments", "inbox", "inbox_outcomes", "transitions")
ACH_STATES = "'created', 'submitted', 'settled', 'available', 'canceled', 'declined'"

UPGRADE = f"""
CREATE TABLE payments.payments (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    payload_fingerprint TEXT NOT NULL CHECK (payload_fingerprint ~ '^[0-9a-f]{{64}}$'),
    provider            TEXT NOT NULL CHECK (provider ~ '^[a-z0-9-]{{1,40}}$'),
    provider_payment_id TEXT NOT NULL CHECK (provider_payment_id <> ''),
    rail                TEXT NOT NULL CHECK (rail = 'ach'),
    direction           TEXT NOT NULL CHECK (direction = 'pull'),
    customer_account    TEXT NOT NULL REFERENCES ledger.accounts (code),
    currency            TEXT NOT NULL CHECK (currency ~ '^[A-Z]{{3,5}}$'),
    amount              BIGINT NOT NULL CHECK (amount > 0),
    description         TEXT NOT NULL DEFAULT '',
    recorded_at         TIMESTAMPTZ NOT NULL,
    UNIQUE (provider, provider_payment_id)
);

CREATE TABLE payments.inbox (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider    TEXT NOT NULL,
    event_id    TEXT NOT NULL CHECK (event_id <> ''),
    body        BYTEA NOT NULL,
    headers     JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    UNIQUE (provider, event_id)
);

CREATE TABLE payments.inbox_outcomes (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inbox_id    BIGINT NOT NULL UNIQUE REFERENCES payments.inbox (id),
    payment_id  BIGINT NOT NULL REFERENCES payments.payments (id),
    outcome     TEXT NOT NULL CHECK (outcome IN ('applied', 'stale', 'exception')),
    detail      TEXT NOT NULL DEFAULT '',
    recorded_at TIMESTAMPTZ NOT NULL,
    CHECK ((outcome = 'exception') = (detail <> ''))
);

CREATE TABLE payments.transitions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id     BIGINT NOT NULL REFERENCES payments.payments (id),
    state          TEXT NOT NULL CHECK (state IN ({ACH_STATES})),
    inbox_id       BIGINT REFERENCES payments.inbox (id),
    job_name       TEXT,
    job_version    TEXT,
    transaction_id BIGINT REFERENCES ledger.transactions (id),
    hold_id        BIGINT REFERENCES ledger.holds (id),
    effective_at   TIMESTAMPTZ NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL,
    UNIQUE (payment_id, state),
    -- The Actor: the Inbox event behind the change, or a system job, never both.
    CHECK (
        (inbox_id IS NOT NULL AND job_name IS NULL AND job_version IS NULL)
        OR (inbox_id IS NULL AND job_name <> '' AND job_version <> '')
    )
);

CREATE FUNCTION payments.reject_history_edits() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('railbench.migrating', true) = 'on' THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'payments.% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

APPEND_ONLY_TRIGGERS = "\n".join(
    f"""
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON payments.{table}
    FOR EACH STATEMENT EXECUTE FUNCTION payments.reject_history_edits();
"""
    for table in TABLES
)

GRANTS = f"""
GRANT USAGE ON SCHEMA payments TO {APP_ROLE};
GRANT SELECT, INSERT ON {", ".join(f"payments.{t}" for t in TABLES)} TO {APP_ROLE};
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(APPEND_ONLY_TRIGGERS)
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute(f"DROP TABLE {', '.join(f'payments.{t}' for t in reversed(TABLES))}")
    op.execute("DROP FUNCTION payments.reject_history_edits()")
    op.execute(f"REVOKE USAGE ON SCHEMA payments FROM {APP_ROLE}")
