"""The Simulator's own state (SPEC §6, ADR 0005), in the `simulator` schema.

The Simulator plays an outside Provider, so this is its private bookkeeping, not part of the
Ledger: rows are updated as Payments advance and webhooks are delivered. The Ledger never
reads these tables.

- `payments`: Payments created through the Simulator's API, unique by Idempotency Key.
- `events`: Provider Events with their frozen webhook body, and delivery state (attempts,
  next attempt, delivered or abandoned).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = """
CREATE TABLE simulator.payments (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,
    payload_fingerprint TEXT NOT NULL,
    rail                TEXT NOT NULL,
    direction           TEXT NOT NULL,
    currency            TEXT NOT NULL,
    amount              BIGINT NOT NULL CHECK (amount > 0),
    reference           TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    submitted_at        TIMESTAMPTZ,
    settles_at          TIMESTAMPTZ,
    settled_at          TIMESTAMPTZ
);

CREATE TABLE simulator.events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id      BIGINT NOT NULL REFERENCES simulator.payments (id),
    type            TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    body            TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    delivered_at    TIMESTAMPTZ,
    abandoned_at    TIMESTAMPTZ,
    last_error      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX events_due ON simulator.events (next_attempt_at)
    WHERE delivered_at IS NULL AND abandoned_at IS NULL;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute("DROP TABLE simulator.events, simulator.payments")
