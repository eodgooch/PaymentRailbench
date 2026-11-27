"""Ledger core: Accounts, Transactions and Entries, balanced and append-only (SPEC §3.1, §3.2).

- Accounts have a code, a type and one currency.
- Entries carry a signed, non-zero BIGINT amount in Minor Units, an Effective Time, a Recorded
  Time (ADR 0002) and an Actor (NOT NULL; its shape is checked per kind). An Entry's currency
  must be its Account's currency (a composite foreign key).
- A Transaction's Entries sum to zero per currency, checked at commit by deferred constraint
  triggers. A Transaction with no Entries fails the same check.
- Each Transaction has a unique Idempotency Key and a fingerprint of the payload it was posted
  with. A Reversal references the Transaction it reverses; `reverses_id` is unique, so a
  Transaction can be reversed once.
- Append-only (ADR 0006): the app role `railbench_app` gets SELECT and INSERT only, and a
  trigger rejects UPDATE, DELETE and TRUNCATE from every role, the owner included, unless the
  session is running migrations (`railbench.migrating`, set by `migrations/env.py`).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "railbench_app"
TABLES = ("accounts", "transactions", "entries")

UPGRADE = """
CREATE TABLE ledger.accounts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9:._-]{0,127}$'),
    type        TEXT NOT NULL CHECK (type IN ('asset', 'liability', 'revenue', 'expense')),
    currency    TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3,5}$'),
    recorded_at TIMESTAMPTZ NOT NULL,
    -- The target of the Entries' (account_id, currency) foreign key.
    UNIQUE (id, currency)
);

CREATE TABLE ledger.transactions (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    payload_fingerprint TEXT NOT NULL CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
    description         TEXT NOT NULL DEFAULT '',
    reverses_id         BIGINT UNIQUE REFERENCES ledger.transactions (id),
    recorded_at         TIMESTAMPTZ NOT NULL,
    CHECK (reverses_id IS DISTINCT FROM id)
);

CREATE TABLE ledger.entries (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id    BIGINT NOT NULL REFERENCES ledger.transactions (id),
    account_id        BIGINT NOT NULL,
    currency          TEXT NOT NULL,
    amount            BIGINT NOT NULL CHECK (amount <> 0),
    effective_at      TIMESTAMPTZ NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL,
    actor_kind        TEXT NOT NULL
                      CHECK (actor_kind IN ('system_job', 'provider_event', 'person')),
    actor_job_name    TEXT,
    actor_job_version TEXT,
    actor_inbox_id    TEXT,
    actor_operator_id TEXT,
    actor_proposal_id TEXT,
    FOREIGN KEY (account_id, currency) REFERENCES ledger.accounts (id, currency),
    -- Each Actor kind has exactly its own fields, all non-empty.
    CONSTRAINT entries_actor_shape CHECK (
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
    )
);

CREATE INDEX entries_transaction_id ON ledger.entries (transaction_id);
CREATE INDEX entries_account_id ON ledger.entries (account_id);

-- Zero-sum per currency, and at least one Entry, checked once per Transaction at commit.
CREATE FUNCTION ledger.check_transaction_balances() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    txn_id BIGINT;
    unbalanced TEXT;
BEGIN
    IF TG_TABLE_NAME = 'entries' THEN
        txn_id := NEW.transaction_id;
    ELSE
        txn_id := NEW.id;
    END IF;
    IF NOT EXISTS (SELECT FROM ledger.entries WHERE transaction_id = txn_id) THEN
        RAISE EXCEPTION 'Transaction % has no Entries', txn_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'transaction_has_entries';
    END IF;
    SELECT string_agg(currency || ' ' || total, ', ') INTO unbalanced
    FROM (
        SELECT currency, sum(amount) AS total
        FROM ledger.entries WHERE transaction_id = txn_id
        GROUP BY currency HAVING sum(amount) <> 0
    ) AS sums;
    IF unbalanced IS NOT NULL THEN
        RAISE EXCEPTION 'Transaction % does not balance: %', txn_id, unbalanced
            USING ERRCODE = 'check_violation', CONSTRAINT = 'transaction_balances';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER transaction_balances
    AFTER INSERT ON ledger.entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger.check_transaction_balances();

CREATE CONSTRAINT TRIGGER transaction_has_entries
    AFTER INSERT ON ledger.transactions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger.check_transaction_balances();

-- History is never edited: corrections are Reversals.
CREATE FUNCTION ledger.reject_history_edits() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('railbench.migrating', true) = 'on' THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'the Ledger is append-only: % on ledger.% is not allowed; post a Reversal',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

APPEND_ONLY_TRIGGERS = "\n".join(
    f"""
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON ledger.{table}
    FOR EACH STATEMENT EXECUTE FUNCTION ledger.reject_history_edits();
"""
    for table in TABLES
)

GRANTS = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        CREATE ROLE {APP_ROLE} NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA ledger TO {APP_ROLE};
GRANT SELECT, INSERT ON {", ".join(f"ledger.{t}" for t in TABLES)} TO {APP_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA ledger GRANT SELECT, INSERT ON TABLES TO {APP_ROLE};
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(APPEND_ONLY_TRIGGERS)
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute(f"DROP TABLE {', '.join(f'ledger.{t}' for t in reversed(TABLES))}")
    op.execute("DROP FUNCTION ledger.check_transaction_balances(), ledger.reject_history_edits()")
    # Removes the role's grants and default privileges in this database, then the role.
    op.execute(f"DROP OWNED BY {APP_ROLE}")
    op.execute(f"DROP ROLE {APP_ROLE}")
