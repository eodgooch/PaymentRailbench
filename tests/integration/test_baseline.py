"""The baseline migration against a real Postgres 16, and /health on top of it."""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from railbench.common.clock import Clock, ManualClock
from railbench.ledger.app import create_app as create_ledger_app
from railbench.simulator.app import create_app as create_simulator_app
from tests.integration.helpers import alembic_config

pytestmark = pytest.mark.integration


def test_migrations_create_a_schema_for_each_part_of_railbench(engine: Engine) -> None:
    with engine.connect() as connection:
        schemas = set(
            connection.scalars(text("SELECT schema_name FROM information_schema.schemata"))
        )

    assert {"ledger", "payments", "reconciliation", "simulator"} <= schemas


def test_baseline_downgrades_and_upgrades_cleanly(database_url: str, engine: Engine) -> None:
    command.downgrade(alembic_config(database_url), "base")
    command.upgrade(alembic_config(database_url), "head")

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
