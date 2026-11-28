"""Reconciliation, Breaks and the Exception Queue (SPEC §7.1, ADR 0015).

In the `reconciliation` schema, append-only like the Ledger:

- `runs`: one Reconciliation of one Provider's Banking Day, at most once per (provider,
  day): the ingested reports it read (the day's and the next Banking Day's), the moment
  whose knowledge it matched (`known_at`), its system-job Actor and when it ran.
- `breaks`: every disagreement a run found, with its Break Type, what it's about
  (`match_key`: a Provider Payment ID, a report line ID or a re-issued report), its dollar
  exposure (non-negative Minor Units, in one currency) and its facts (expected and reported
  day, amount, state), detected at the run's Recorded Time. Its id is a hash of (provider,
  day, Break Type, match key), so finding the same disagreement again is the same Break.
- `break_evidence`: the rows a Break rests on, one per row, each a foreign key to exactly
  one of a Payment, a state change, an Inbox event, a Ledger Transaction, a Settlement
  Report, a report line or a Provider Adjustment decision.
- `break_resolutions`: at most one per Break: the person who resolved it (an operator ID,
  and the approved Proposal ID when it came from one) and exactly one of a correcting
  Transaction in the Ledger or a reason.
- `exception_queue`: a view of the Breaks with no resolution.

The app role gets USAGE on the schema, SELECT and INSERT on the four tables and SELECT on the
view, explicitly (no default privileges, ADR 0008), and each table an append-only trigger.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "railbench_app"
TABLES = ("runs", "breaks", "break_evidence", "break_resolutions")
BREAK_TYPES = (
    "'missing_in_ledger', 'missing_at_provider', 'amount_mismatch', "
    "'timing_mismatch', 'duplicate', 'status_mismatch'"
)

UPGRADE = f"""
CREATE TABLE reconciliation.runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider       TEXT NOT NULL CHECK (provider ~ '^[a-z0-9-]{{1,40}}$'),
    banking_day    DATE NOT NULL,
    report_id      BIGINT NOT NULL REFERENCES payments.settlement_reports (id),
    next_report_id BIGINT NOT NULL REFERENCES payments.settlement_reports (id),
    known_at       TIMESTAMPTZ NOT NULL,
    job_name       TEXT NOT NULL CHECK (job_name <> ''),
    job_version    TEXT NOT NULL CHECK (job_version <> ''),
    recorded_at    TIMESTAMPTZ NOT NULL,
    UNIQUE (provider, banking_day),
    CHECK (report_id <> next_report_id),
    CHECK (known_at <= recorded_at)
);

CREATE TABLE reconciliation.breaks (
    id          TEXT PRIMARY KEY CHECK (id ~ '^brk_[0-9a-f]{{24}}$'),
    run_id      BIGINT NOT NULL REFERENCES reconciliation.runs (id),
    provider    TEXT NOT NULL,
    banking_day DATE NOT NULL,
    break_type  TEXT NOT NULL CHECK (break_type IN ({BREAK_TYPES})),
    match_key   TEXT NOT NULL CHECK (match_key <> ''),
    exposure    BIGINT NOT NULL CHECK (exposure >= 0),
    currency    TEXT NOT NULL CHECK (currency ~ '^[A-Z]{{3,5}}$'),
    facts       JSONB NOT NULL CHECK (jsonb_typeof(facts) = 'object'),
    detected_at TIMESTAMPTZ NOT NULL,
    UNIQUE (provider, banking_day, break_type, match_key)
);
CREATE INDEX breaks_by_day ON reconciliation.breaks (banking_day, provider);

CREATE TABLE reconciliation.break_evidence (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    break_id               TEXT NOT NULL REFERENCES reconciliation.breaks (id),
    payment_id             BIGINT REFERENCES payments.payments (id),
    transition_id          BIGINT REFERENCES payments.transitions (id),
    inbox_id               BIGINT REFERENCES payments.inbox (id),
    transaction_id         BIGINT REFERENCES ledger.transactions (id),
    report_id              BIGINT REFERENCES payments.settlement_reports (id),
    report_line_id         BIGINT REFERENCES payments.settlement_report_lines (id),
    provider_adjustment_id BIGINT REFERENCES payments.provider_adjustments (id),
    CHECK (
        num_nonnulls(
            payment_id, transition_id, inbox_id, transaction_id, report_id, report_line_id,
            provider_adjustment_id
        ) = 1
    )
);
CREATE INDEX break_evidence_by_break ON reconciliation.break_evidence (break_id);

CREATE TABLE reconciliation.break_resolutions (
    id                        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    break_id                  TEXT NOT NULL UNIQUE REFERENCES reconciliation.breaks (id),
    actor_operator_id         TEXT NOT NULL CHECK (actor_operator_id <> ''),
    actor_proposal_id         TEXT CHECK (actor_proposal_id <> ''),
    correcting_transaction_id BIGINT REFERENCES ledger.transactions (id),
    reason                    TEXT CHECK (reason <> ''),
    recorded_at               TIMESTAMPTZ NOT NULL,
    -- Exactly one: a linked correcting Transaction, or a reason.
    CHECK (num_nonnulls(correcting_transaction_id, reason) = 1)
);

CREATE VIEW reconciliation.exception_queue AS
SELECT b.*
FROM reconciliation.breaks b
WHERE NOT EXISTS (
    SELECT 1 FROM reconciliation.break_resolutions r WHERE r.break_id = b.id
);

CREATE FUNCTION reconciliation.reject_history_edits() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('railbench.migrating', true) = 'on' THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'reconciliation.% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

APPEND_ONLY_TRIGGERS = "\n".join(
    f"""
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON reconciliation.{table}
    FOR EACH STATEMENT EXECUTE FUNCTION reconciliation.reject_history_edits();
"""
    for table in TABLES
)

GRANTS = f"""
GRANT USAGE ON SCHEMA reconciliation TO {APP_ROLE};
GRANT SELECT, INSERT ON {", ".join(f"reconciliation.{t}" for t in TABLES)} TO {APP_ROLE};
GRANT SELECT ON reconciliation.exception_queue TO {APP_ROLE};
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(APPEND_ONLY_TRIGGERS)
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW reconciliation.exception_queue")
    op.execute(f"DROP TABLE {', '.join(f'reconciliation.{t}' for t in reversed(TABLES))}")
    op.execute("DROP FUNCTION reconciliation.reject_history_edits()")
    op.execute(f"REVOKE USAGE ON SCHEMA reconciliation FROM {APP_ROLE}")
