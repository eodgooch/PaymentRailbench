"""The baseline migration against a real Postgres 16, and /health on top of it."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer

from railbench.common.clock import Clock, ManualClock
from railbench.ledger.app import create_app as create_ledger_app
from railbench.simulator.app import create_app as create_simulator_app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        url: str = postgres.get_connection_url()
        command.upgrade(_alembic_config(url), "head")
        yield url


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


def _alembic_config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migrations_create_a_schema_for_each_part_of_railbench(engine: Engine) -> None:
    with engine.connect() as connection:
        schemas = set(
            connection.scalars(text("SELECT schema_name FROM information_schema.schemata"))
        )

    assert {"ledger", "payments", "reconciliation", "simulator"} <= schemas


def test_baseline_downgrades_and_upgrades_cleanly(database_url: str, engine: Engine) -> None:
    command.downgrade(_alembic_config(database_url), "base")
    command.upgrade(_alembic_config(database_url), "head")

    with engine.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.schemata WHERE schema_name IN "
                "('ledger', 'payments', 'reconciliation', 'simulator')"
            )
        )
    assert count == 4


@pytest.mark.parametrize("create_app", [create_ledger_app, create_simulator_app])
def test_health_reports_the_database_ok_against_postgres(
    create_app: Callable[[Engine, Clock], FastAPI], engine: Engine
) -> None:
    client = TestClient(create_app(engine, ManualClock(datetime(2026, 3, 2, tzinfo=UTC))))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["database"] == "ok"
