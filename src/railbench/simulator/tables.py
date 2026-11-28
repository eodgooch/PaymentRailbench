"""SQLAlchemy Core views of the Simulator's tables (migration 0005 is the source of truth)."""

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, Table, Text

from railbench.common.db import SIMULATOR_SCHEMA

metadata = MetaData(schema=SIMULATOR_SCHEMA)


def _time(name: str, nullable: bool = True) -> Column[object]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


payments = Table(
    "payments",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("payload_fingerprint", Text, nullable=False),
    Column("rail", Text, nullable=False),
    Column("direction", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("reference", Text, nullable=False),
    Column("status", Text, nullable=False),
    _time("created_at", nullable=False),
    _time("submitted_at"),
    _time("settles_at"),
    _time("settled_at"),
)

events = Table(
    "events",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("payment_id", BigInteger, nullable=False),
    Column("type", Text, nullable=False),
    _time("occurred_at", nullable=False),
    Column("body", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    _time("next_attempt_at", nullable=False),
    _time("delivered_at"),
    _time("abandoned_at"),
    Column("last_error", Text, nullable=False),
)
