"""Shared helpers for the integration tests."""

from uuid import uuid4

from alembic.config import Config

APP_ROLE = "railbench_app"


def alembic_config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def unique(prefix: str) -> str:
    """A code or Idempotency Key no other test uses, so tests can share one database."""
    return f"{prefix}-{uuid4().hex[:12]}"
