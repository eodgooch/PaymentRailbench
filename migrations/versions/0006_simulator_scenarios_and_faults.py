"""Scripted outcomes, delivery Faults and the Fault Manifest, in the `simulator` schema
(SPEC §6, ADR 0011).

- `plans`: what a Scenario told the Simulator to do with the Payment it will create under an
  Idempotency Key: how it ends (settle, decline, cancel) and which delivery Faults to inject.
- `payments.outcome` and `payments.ended_at`: the planned ending, and when a decline or
  cancellation happened. `payments.public_id`: the Simulator's ID for the Payment, derived
  from its Idempotency Key (existing rows keep their `simpay_<id>`).
- `events.event_id`: the Provider event ID, derived from the Payment's Idempotency Key and
  the event type rather than a database sequence, so a Scenario run names its events the
  same way every time. An `events` row is now one delivery stream: a duplicate Fault adds
  more rows with the same event ID and body. `after_event_id` holds a row back until that
  event has been delivered (out-of-order delivery). `fault_id` names the Fault that added or
  held the row.
- `faults`: the Fault Manifest, one row per injected Fault with its true root cause.

The Ledger's app role has no USAGE on the `simulator` schema, so the Ledger can't read the
manifest (a test proves it).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = """
CREATE TABLE simulator.plans (
    idempotency_key TEXT PRIMARY KEY,
    outcome         TEXT NOT NULL CHECK (outcome IN ('settle', 'decline', 'cancel')),
    faults          JSONB NOT NULL
);

ALTER TABLE simulator.payments
    ADD COLUMN outcome   TEXT NOT NULL DEFAULT 'settle'
        CHECK (outcome IN ('settle', 'decline', 'cancel')),
    ADD COLUMN ended_at  TIMESTAMPTZ,
    ADD COLUMN public_id TEXT;
UPDATE simulator.payments SET public_id = 'simpay_' || id;
ALTER TABLE simulator.payments ALTER COLUMN public_id SET NOT NULL;
ALTER TABLE simulator.payments ADD CONSTRAINT payments_public_id_key UNIQUE (public_id);

ALTER TABLE simulator.events
    ADD COLUMN event_id       TEXT,
    ADD COLUMN after_event_id TEXT,
    ADD COLUMN fault_id       TEXT;
UPDATE simulator.events SET event_id = 'simevt_' || id;
ALTER TABLE simulator.events ALTER COLUMN event_id SET NOT NULL;
CREATE INDEX events_by_event_id ON simulator.events (event_id);

CREATE TABLE simulator.faults (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fault_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payment_id  BIGINT NOT NULL REFERENCES simulator.payments (id),
    event_id    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    params      JSONB NOT NULL,
    injected_at TIMESTAMPTZ NOT NULL,
    cause_code  TEXT NOT NULL,
    root_cause  TEXT NOT NULL,
    UNIQUE (payment_id, fault_id)
);
"""

DOWNGRADE = """
DROP TABLE simulator.faults;
DROP INDEX simulator.events_by_event_id;
ALTER TABLE simulator.events
    DROP COLUMN fault_id, DROP COLUMN after_event_id, DROP COLUMN event_id;
ALTER TABLE simulator.payments DROP COLUMN public_id, DROP COLUMN ended_at, DROP COLUMN outcome;
DROP TABLE simulator.plans;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
