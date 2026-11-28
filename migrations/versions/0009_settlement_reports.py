"""Settlement Reports and Provider Adjustments (SPEC §4.5, §6, ADR 0014).

In the `payments` schema, append-only like the Inbox:

- `settlement_reports`: every Settlement Report the Ledger fetched from a Provider, raw
  (body bytes and response headers), with its SHA-256, unique by (provider, report date,
  SHA-256), so fetching the same report again stores nothing. Its outcome: `ingested` (at
  most one per provider and report date), `conflict` (a different report for a day already
  ingested: kept as evidence, nothing else done) or `malformed` (not a report the Provider
  adapter can read, with why).
- `settlement_report_lines`: an ingested report's lines as the Provider stated them,
  structured, in file order: settlements and Provider Adjustments. They are evidence for
  Reconciliation; ingesting one changes no balance.
- `provider_adjustments`: what the Ledger did with each Provider Adjustment line, appended:
  `pending` (its Payment is known and agrees with the line but hasn't settled in the Ledger
  yet; at most once per line), then at most one decision, `booked` against the original
  Payment by the named Transaction, or `unmatched` (no such Payment, one that contradicts
  the line, or one that ended without settling) with why.

In the `simulator` schema, `settlement_reports`: each report the Simulator published, frozen
as published, one per (provider, Banking Day).

The app role gets SELECT and INSERT on the three `payments` tables, explicitly (no default
privileges, ADR 0008), and each gets the schema's append-only trigger. It still has no
access to the `simulator` schema.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "railbench_app"
TABLES = ("settlement_reports", "settlement_report_lines", "provider_adjustments")

UPGRADE = """
CREATE TABLE payments.settlement_reports (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider    TEXT NOT NULL CHECK (provider ~ '^[a-z0-9-]{1,40}$'),
    report_date DATE NOT NULL,
    body        BYTEA NOT NULL,
    body_sha256 TEXT NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    headers     JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('ingested', 'conflict', 'malformed')),
    detail      TEXT NOT NULL DEFAULT '',
    UNIQUE (provider, report_date, body_sha256),
    CHECK ((outcome = 'ingested') = (detail = ''))
);
CREATE UNIQUE INDEX settlement_reports_ingested_once
    ON payments.settlement_reports (provider, report_date) WHERE outcome = 'ingested';

CREATE TABLE payments.settlement_report_lines (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id           BIGINT NOT NULL REFERENCES payments.settlement_reports (id),
    position            INTEGER NOT NULL CHECK (position > 0),
    line_id             TEXT NOT NULL CHECK (line_id <> ''),
    type                TEXT NOT NULL CHECK (type IN ('settlement', 'adjustment')),
    provider_payment_id TEXT NOT NULL CHECK (provider_payment_id <> ''),
    idempotency_key     TEXT NOT NULL,
    rail                TEXT NOT NULL CHECK (rail IN ('ach', 'rtp')),
    direction           TEXT NOT NULL CHECK (direction IN ('pull', 'push')),
    currency            TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3,5}$'),
    amount              BIGINT NOT NULL CHECK (amount <> 0),
    settled_at          TIMESTAMPTZ NOT NULL,
    original_line_id    TEXT,
    reason              TEXT,
    UNIQUE (report_id, line_id),
    UNIQUE (report_id, position),
    CHECK (
        (type = 'adjustment') = (original_line_id IS NOT NULL AND reason IS NOT NULL)
        AND (type = 'settlement') = (original_line_id IS NULL AND reason IS NULL)
    )
);

CREATE TABLE payments.provider_adjustments (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    line_id        BIGINT NOT NULL REFERENCES payments.settlement_report_lines (id),
    outcome        TEXT NOT NULL CHECK (outcome IN ('pending', 'booked', 'unmatched')),
    payment_id     BIGINT REFERENCES payments.payments (id),
    transaction_id BIGINT UNIQUE REFERENCES ledger.transactions (id),
    detail         TEXT NOT NULL DEFAULT '',
    recorded_at    TIMESTAMPTZ NOT NULL,
    CHECK (
        (outcome = 'booked') = (payment_id IS NOT NULL AND transaction_id IS NOT NULL)
        AND (outcome = 'booked' OR (transaction_id IS NULL AND detail <> ''))
        AND (outcome <> 'pending' OR payment_id IS NOT NULL)
    )
);
-- At most one `pending` row per line, and at most one final one (`booked` or `unmatched`).
CREATE UNIQUE INDEX provider_adjustments_pending_once
    ON payments.provider_adjustments (line_id) WHERE outcome = 'pending';
CREATE UNIQUE INDEX provider_adjustments_decided_once
    ON payments.provider_adjustments (line_id) WHERE outcome <> 'pending';

CREATE TABLE simulator.settlement_reports (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider     TEXT NOT NULL,
    report_date  DATE NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    body         BYTEA NOT NULL,
    UNIQUE (provider, report_date)
);
"""

APPEND_ONLY_TRIGGERS = "\n".join(
    f"""
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON payments.{table}
    FOR EACH STATEMENT EXECUTE FUNCTION payments.reject_history_edits();
"""
    for table in TABLES
)

GRANTS = f"GRANT SELECT, INSERT ON {', '.join(f'payments.{t}' for t in TABLES)} TO {APP_ROLE};"


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(APPEND_ONLY_TRIGGERS)
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute("DROP TABLE simulator.settlement_reports")
    op.execute(f"DROP TABLE {', '.join(f'payments.{t}' for t in reversed(TABLES))}")
