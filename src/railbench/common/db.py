"""The database Engine and the Postgres schemas each part of Railbench owns."""

from sqlalchemy import Engine, create_engine

LEDGER_SCHEMA = "ledger"
PAYMENTS_SCHEMA = "payments"  # Payments and the Inbox of Provider Events
RECONCILIATION_SCHEMA = "reconciliation"
SIMULATOR_SCHEMA = "simulator"

SCHEMAS = (LEDGER_SCHEMA, PAYMENTS_SCHEMA, RECONCILIATION_SCHEMA, SIMULATOR_SCHEMA)


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
