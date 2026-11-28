"""SQLAlchemy Core views of the `marketplace` schema (migration 0007 is the source of truth)."""

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, Table, Text

from railbench.common.db import MARKETPLACE_SCHEMA

metadata = MetaData(schema=MARKETPLACE_SCHEMA)

auction_closes = Table(
    "auction_closes",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("auction_id", Text, nullable=False, unique=True),
    Column("payload_fingerprint", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=False),
    Column("seller_fee_bps", Integer, nullable=False),
    Column("instant_payout_limit", BigInteger, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)

lots = Table(
    "lots",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("auction_close_id", BigInteger, nullable=False),
    Column("lot_id", Text, nullable=False),
    Column("buyer", Text, nullable=False),
    Column("seller", Text, nullable=False),
    Column("hammer_price", BigInteger, nullable=False),
    Column("fee", BigInteger, nullable=False),
    Column("transaction_id", BigInteger, nullable=False),
)

payouts = Table(
    "payouts",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("auction_close_id", BigInteger, nullable=False),
    Column("customer_account", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("requested", Text, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)

payout_steps = Table(
    "payout_steps",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("payout_id", BigInteger, nullable=False),
    Column("step", Text, nullable=False),
    Column("detail", Text, nullable=False),
    Column("hold_id", BigInteger),
    Column("job_name", Text, nullable=False),
    Column("job_version", Text, nullable=False),
    Column("effective_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)
