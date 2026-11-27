"""Baseline: one Postgres schema per part of Railbench (SPEC §2).

- ledger: Accounts, Transactions and Entries
- payments: Payments and the Inbox of Provider Events
- reconciliation: Settlement Report matching, Breaks and the Exception Queue
- simulator: the Simulator's own state (it plays every Provider, ADR 0005)

Revision ID: 0001
Revises:
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported: a migration must not change when app code does.
SCHEMAS = ("ledger", "payments", "reconciliation", "simulator")


def upgrade() -> None:
    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def downgrade() -> None:
    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema}")
