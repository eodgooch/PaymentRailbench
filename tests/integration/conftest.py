"""One throwaway Postgres 16 for the whole integration run, migrated to head."""

from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine
from testcontainers.community.postgres import PostgresContainer

from railbench.common.db import LEDGER_APP_ROLE, make_engine
from tests.integration.helpers import alembic_config


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        url: str = postgres.get_connection_url()
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    """Connects as the database owner (a superuser in the container)."""
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database_url: str) -> Iterator[Engine]:
    """Connects the way the Ledger API does: as the append-only app role."""
    engine = make_engine(database_url, role=LEDGER_APP_ROLE)
    yield engine
    engine.dispose()
