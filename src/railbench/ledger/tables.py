"""SQLAlchemy Core views of the Ledger tables. Migration 0002 is the source of truth for the
schema, its constraints and its triggers; these definitions only name the columns."""

from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text

from railbench.common.db import LEDGER_SCHEMA

metadata = MetaData(schema=LEDGER_SCHEMA)

accounts = Table(
    "accounts",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("type", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)

transactions = Table(
    "transactions",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("payload_fingerprint", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("reverses_id", BigInteger, unique=True),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)

entries = Table(
    "entries",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("transaction_id", BigInteger, nullable=False),
    Column("account_id", BigInteger, nullable=False),
    Column("currency", Text, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("effective_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("actor_kind", Text, nullable=False),
    Column("actor_job_name", Text),
    Column("actor_job_version", Text),
    Column("actor_inbox_id", Text),
    Column("actor_operator_id", Text),
    Column("actor_proposal_id", Text),
)
