"""Returns, Recovery Receivable and return exceptions (SPEC §4.1-4.3, ADR 0013).

- `payments.transitions.state` may now be `returned`: an ACH pull sent back with a NACHA
  return code, after it settled.
- `payments.return_exceptions`: one row per deficit a Return moved to Recovery Receivable
  (usually one per Return; more when the Customer Balance dips again later, ADR 0013),
  append-only like the rest of the schema. It names the Payment, the Inbox event behind it,
  the Customer Balance and its Recovery Receivable, the return code, the exposure in Minor
  Units, whether it was a Late Return (by time: after the funds became Available), the
  Transaction that moved the deficit and when it took effect. It is an exception record
  for people to work, not a Reconciliation Break.
- `simulator.payments`: the Return the Simulator will make or has made (`return_code`,
  `returns_at`, `returned_at`).

The app role gets SELECT and INSERT on the new table, explicitly (no default privileges,
ADR 0008), and it gets the schema's append-only trigger.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "railbench_app"
STATES = "'created', 'submitted', 'settled', 'available', 'canceled', 'declined'"

UPGRADE = f"""
ALTER TABLE payments.transitions
    DROP CONSTRAINT transitions_state_check,
    ADD CONSTRAINT transitions_state_check CHECK (state IN ({STATES}, 'returned'));

CREATE TABLE payments.return_exceptions (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id              BIGINT NOT NULL REFERENCES payments.payments (id),
    inbox_id                BIGINT NOT NULL REFERENCES payments.inbox (id),
    customer_account        TEXT NOT NULL REFERENCES ledger.accounts (code),
    recovery_account        TEXT NOT NULL REFERENCES ledger.accounts (code),
    currency                TEXT NOT NULL CHECK (currency ~ '^[A-Z]{{3,5}}$'),
    return_code             TEXT NOT NULL CHECK (return_code ~ '^R[0-9]{{2}}$'),
    exposure                BIGINT NOT NULL CHECK (exposure > 0),
    late                    BOOLEAN NOT NULL,
    recovery_transaction_id BIGINT NOT NULL UNIQUE REFERENCES ledger.transactions (id),
    detail                  TEXT NOT NULL CHECK (detail <> ''),
    effective_at            TIMESTAMPTZ NOT NULL,
    recorded_at             TIMESTAMPTZ NOT NULL
);

CREATE TRIGGER return_exceptions_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON payments.return_exceptions
    FOR EACH STATEMENT EXECUTE FUNCTION payments.reject_history_edits();

GRANT SELECT, INSERT ON payments.return_exceptions TO {APP_ROLE};

ALTER TABLE simulator.payments
    ADD COLUMN return_code TEXT,
    ADD COLUMN returns_at  TIMESTAMPTZ,
    ADD COLUMN returned_at TIMESTAMPTZ,
    ADD CONSTRAINT payments_return_is_planned_whole
        CHECK ((return_code IS NULL) = (returns_at IS NULL));
"""

DOWNGRADE = f"""
ALTER TABLE simulator.payments
    DROP CONSTRAINT payments_return_is_planned_whole,
    DROP COLUMN returned_at,
    DROP COLUMN returns_at,
    DROP COLUMN return_code;
DROP TABLE payments.return_exceptions;
ALTER TABLE payments.transitions
    DROP CONSTRAINT transitions_state_check,
    ADD CONSTRAINT transitions_state_check CHECK (state IN ({STATES}));
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
