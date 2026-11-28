"""Bitemporal balances and Holds: pending Transactions, Settlements, Holds (SPEC §3.3, ADR 0009).

- A Transaction is posted or pending, fixed when it is posted. Existing Transactions are
  posted. A Reversal takes the status of the Transaction it reverses.
- A Settlement turns a pending Transaction's Entries from Pending to Posted without touching
  them: an append-only row with its own Effective and Recorded Time, Actor and Idempotency
  Key. It references a pending Transaction (a composite foreign key on `(id, status)`), at
  most once.
- A Hold reserves a positive amount on one Account, in its currency. It ends at most once
  (`hold_ends.hold_id` is unique): released, with its own Idempotency Key, or captured, by
  posting a Transaction in the same commit.
- Every new table gets the Actor shape check from 0002, SELECT and INSERT for the app role,
  granted here explicitly (there are no default privileges), and the append-only trigger.
- Entries are indexed by (account_id, effective_at, recorded_at) for the balance query;
  that index replaces 0002's index on account_id alone.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "railbench_app"
TABLES = ("settlements", "holds", "hold_ends")

ACTOR_COLUMNS = """
    actor_kind        TEXT NOT NULL
                      CHECK (actor_kind IN ('system_job', 'provider_event', 'person')),
    actor_job_name    TEXT,
    actor_job_version TEXT,
    actor_inbox_id    TEXT,
    actor_operator_id TEXT,
    actor_proposal_id TEXT,
    CHECK (
        CASE actor_kind
        WHEN 'system_job' THEN
            actor_job_name <> '' AND actor_job_version <> ''
            AND num_nonnulls(actor_inbox_id, actor_operator_id, actor_proposal_id) = 0
        WHEN 'provider_event' THEN
            actor_inbox_id <> ''
            AND num_nonnulls(actor_job_name, actor_job_version,
                             actor_operator_id, actor_proposal_id) = 0
        WHEN 'person' THEN
            actor_operator_id <> '' AND actor_proposal_id <> ''
            AND num_nonnulls(actor_job_name, actor_job_version, actor_inbox_id) = 0
        END
    )"""

IDEMPOTENCY_COLUMNS = """
    idempotency_key     TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    payload_fingerprint TEXT NOT NULL CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$')"""

UPGRADE = f"""
ALTER TABLE ledger.transactions
    ADD COLUMN status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('pending', 'posted')),
    ADD CONSTRAINT transactions_id_status_key UNIQUE (id, status);
ALTER TABLE ledger.transactions ALTER COLUMN status DROP DEFAULT;

CREATE INDEX entries_account_times ON ledger.entries (account_id, effective_at, recorded_at);
DROP INDEX ledger.entries_account_id;

CREATE TABLE ledger.settlements (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id      BIGINT NOT NULL UNIQUE,
    transaction_status  TEXT NOT NULL DEFAULT 'pending' CHECK (transaction_status = 'pending'),
    {IDEMPOTENCY_COLUMNS},
    effective_at        TIMESTAMPTZ NOT NULL,
    recorded_at         TIMESTAMPTZ NOT NULL,
    {ACTOR_COLUMNS},
    -- Only a pending Transaction can settle.
    FOREIGN KEY (transaction_id, transaction_status)
        REFERENCES ledger.transactions (id, status)
);

CREATE TABLE ledger.holds (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    {IDEMPOTENCY_COLUMNS},
    account_id          BIGINT NOT NULL,
    currency            TEXT NOT NULL,
    amount              BIGINT NOT NULL CHECK (amount > 0),
    description         TEXT NOT NULL DEFAULT '',
    effective_at        TIMESTAMPTZ NOT NULL,
    recorded_at         TIMESTAMPTZ NOT NULL,
    {ACTOR_COLUMNS},
    FOREIGN KEY (account_id, currency) REFERENCES ledger.accounts (id, currency)
);

CREATE INDEX holds_account_times ON ledger.holds (account_id, effective_at, recorded_at);

-- How a Hold ended: released (with its own Idempotency Key) or captured (by a Transaction,
-- which carries the key). A Hold ends at most once.
CREATE TABLE ledger.hold_ends (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hold_id             BIGINT NOT NULL UNIQUE REFERENCES ledger.holds (id),
    outcome             TEXT NOT NULL CHECK (outcome IN ('released', 'captured')),
    transaction_id      BIGINT UNIQUE REFERENCES ledger.transactions (id),
    idempotency_key     TEXT UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    payload_fingerprint TEXT CHECK (payload_fingerprint ~ '^[0-9a-f]{{64}}$'),
    effective_at        TIMESTAMPTZ NOT NULL,
    recorded_at         TIMESTAMPTZ NOT NULL,
    {ACTOR_COLUMNS},
    CONSTRAINT hold_ends_outcome_shape CHECK (
        CASE outcome
        WHEN 'released' THEN
            transaction_id IS NULL
            AND idempotency_key IS NOT NULL AND payload_fingerprint IS NOT NULL
        WHEN 'captured' THEN
            transaction_id IS NOT NULL
            AND idempotency_key IS NULL AND payload_fingerprint IS NULL
        END
    )
);
"""

APPEND_ONLY_TRIGGERS = "\n".join(
    f"""
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON ledger.{table}
    FOR EACH STATEMENT EXECUTE FUNCTION ledger.reject_history_edits();
"""
    for table in TABLES
)

GRANTS = f"GRANT SELECT, INSERT ON {', '.join(f'ledger.{t}' for t in TABLES)} TO {APP_ROLE};"


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(APPEND_ONLY_TRIGGERS)
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute(f"DROP TABLE {', '.join(f'ledger.{t}' for t in reversed(TABLES))}")
    op.execute("CREATE INDEX entries_account_id ON ledger.entries (account_id)")
    op.execute("DROP INDEX ledger.entries_account_times")
    op.execute("ALTER TABLE ledger.transactions DROP CONSTRAINT transactions_id_status_key")
    op.execute("ALTER TABLE ledger.transactions DROP COLUMN status")
