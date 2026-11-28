"""SQLAlchemy Core views of the `payments` schema (migrations 0004, 0007 and 0008 are the source of
truth)."""

from sqlalchemy import BigInteger, Boolean, Column, DateTime, LargeBinary, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB

from railbench.common.db import PAYMENTS_SCHEMA

metadata = MetaData(schema=PAYMENTS_SCHEMA)

payments = Table(
    "payments",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("payload_fingerprint", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("provider_payment_id", Text, nullable=False),
    Column("rail", Text, nullable=False),
    Column("direction", Text, nullable=False),
    Column("customer_account", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("description", Text, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("hold_id", BigInteger),
)

inbox = Table(
    "inbox",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("provider", Text, nullable=False),
    Column("event_id", Text, nullable=False),
    Column("body", LargeBinary, nullable=False),
    Column("headers", JSONB, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
)

inbox_outcomes = Table(
    "inbox_outcomes",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("inbox_id", BigInteger, nullable=False, unique=True),
    Column("payment_id", BigInteger, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("detail", Text, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)

transitions = Table(
    "transitions",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("payment_id", BigInteger, nullable=False),
    Column("state", Text, nullable=False),
    Column("inbox_id", BigInteger),
    Column("job_name", Text),
    Column("job_version", Text),
    Column("transaction_id", BigInteger),
    Column("hold_id", BigInteger),
    Column("effective_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)

return_exceptions = Table(
    "return_exceptions",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("payment_id", BigInteger, nullable=False, unique=True),
    Column("inbox_id", BigInteger, nullable=False),
    Column("customer_account", Text, nullable=False),
    Column("recovery_account", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("return_code", Text, nullable=False),
    Column("exposure", BigInteger, nullable=False),
    Column("late", Boolean, nullable=False),
    Column("recovery_transaction_id", BigInteger, nullable=False),
    Column("detail", Text, nullable=False),
    Column("effective_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)
