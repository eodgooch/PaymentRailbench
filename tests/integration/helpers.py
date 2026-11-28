"""Shared helpers for the integration tests."""

from datetime import UTC, datetime
from uuid import uuid4

from alembic.config import Config

from railbench.ledger.journal import Journal


def alembic_config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def unique(prefix: str) -> str:
    """A code or Idempotency Key no other test uses, so tests can share one database."""
    return f"{prefix}-{uuid4().hex[:12]}"


def posted_balance(journal: Journal, code: str) -> int:
    """The Posted Balance counting every Entry known now, whatever its Effective Time, in the
    Account's normal direction."""
    return journal.balances(code, END_OF_TIME).posted


END_OF_TIME = datetime(9999, 1, 1, tzinfo=UTC)
