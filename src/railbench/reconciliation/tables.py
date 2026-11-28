"""SQLAlchemy Core views of the `reconciliation` schema (migration 0010 is the source of
truth)."""

from sqlalchemy import BigInteger, Column, Date, DateTime, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB

from railbench.common.db import RECONCILIATION_SCHEMA

metadata = MetaData(schema=RECONCILIATION_SCHEMA)

runs = Table(
    "runs",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("provider", Text, nullable=False),
    Column("banking_day", Date, nullable=False),
    Column("report_id", BigInteger, nullable=False),
    Column("next_report_id", BigInteger, nullable=False),
    Column("known_at", DateTime(timezone=True), nullable=False),
    Column("job_name", Text, nullable=False),
    Column("job_version", Text, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)

breaks = Table(
    "breaks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", BigInteger, nullable=False),
    Column("provider", Text, nullable=False),
    Column("banking_day", Date, nullable=False),
    Column("break_type", Text, nullable=False),
    Column("match_key", Text, nullable=False),
    Column("exposure", BigInteger, nullable=False),
    Column("currency", Text, nullable=False),
    Column("facts", JSONB, nullable=False),
    Column("detected_at", DateTime(timezone=True), nullable=False),
)

# The evidence columns, by the `Evidence` field they hold.
EVIDENCE_COLUMNS = {
    "payments": "payment_id",
    "transitions": "transition_id",
    "inbox": "inbox_id",
    "transactions": "transaction_id",
    "reports": "report_id",
    "report_lines": "report_line_id",
    "provider_adjustments": "provider_adjustment_id",
}

break_evidence = Table(
    "break_evidence",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("break_id", Text, nullable=False),
    *(Column(column, BigInteger) for column in EVIDENCE_COLUMNS.values()),
)

break_resolutions = Table(
    "break_resolutions",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("break_id", Text, nullable=False, unique=True),
    Column("actor_operator_id", Text, nullable=False),
    Column("actor_proposal_id", Text),
    Column("correcting_transaction_id", BigInteger),
    Column("reason", Text),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)
