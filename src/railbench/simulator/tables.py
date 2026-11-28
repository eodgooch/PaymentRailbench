"""SQLAlchemy Core views of the Simulator's tables (migrations 0005-0007 are the source of
truth)."""

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB

from railbench.common.db import SIMULATOR_SCHEMA

metadata = MetaData(schema=SIMULATOR_SCHEMA)


def _time(name: str, nullable: bool = True) -> Column[object]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


payments = Table(
    "payments",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("public_id", Text, nullable=False, unique=True),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("payload_fingerprint", Text, nullable=False),
    Column("rail", Text, nullable=False),
    Column("direction", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("reference", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    _time("created_at", nullable=False),
    _time("submitted_at"),
    _time("settles_at"),
    _time("settled_at"),
    _time("ended_at"),
)

events = Table(
    "events",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("payment_id", BigInteger, nullable=False),
    Column("event_id", Text, nullable=False),
    Column("after_event_id", Text),
    Column("fault_id", Text),
    Column("type", Text, nullable=False),
    _time("occurred_at", nullable=False),
    Column("body", Text, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    _time("next_attempt_at", nullable=False),
    _time("delivered_at"),
    _time("abandoned_at"),
    Column("last_error", Text, nullable=False, default=""),
)

plans = Table(
    "plans",
    metadata,
    Column("idempotency_key", Text, primary_key=True),
    Column("rail", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("faults", JSONB, nullable=False),
)

faults = Table(
    "faults",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("fault_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("payment_id", BigInteger, nullable=False),
    Column("event_id", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("params", JSONB, nullable=False),
    _time("injected_at", nullable=False),
    Column("cause_code", Text, nullable=False),
    Column("root_cause", Text, nullable=False),
)
