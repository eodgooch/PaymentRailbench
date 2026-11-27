from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from railbench.common.clock import Clock, ManualClock
from railbench.ledger.app import create_app as create_ledger_app
from railbench.simulator.app import create_app as create_simulator_app

AppFactory = Callable[[Engine, Clock], FastAPI]

SERVICES = [
    pytest.param(create_ledger_app, "ledger", id="ledger"),
    pytest.param(create_simulator_app, "simulator", id="simulator"),
]

CLOSE_OF_BUSINESS = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)


@pytest.fixture
def reachable_db() -> Engine:
    return create_engine("sqlite://")


@pytest.fixture
def unreachable_db() -> Engine:
    # Nothing listens on port 1, so every connection attempt is refused.
    return create_engine(
        "postgresql+psycopg://railbench:railbench@127.0.0.1:1/railbench",
        connect_args={"connect_timeout": 1},
    )


@pytest.mark.parametrize(("create_app", "service"), SERVICES)
def test_health_is_ok_when_the_database_answers(
    create_app: AppFactory, service: str, reachable_db: Engine
) -> None:
    client = TestClient(create_app(reachable_db, ManualClock(CLOSE_OF_BUSINESS)))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": service,
        "status": "ok",
        "database": "ok",
        "checked_at": "2026-03-02T21:00:00Z",
    }


@pytest.mark.parametrize(("create_app", "service"), SERVICES)
def test_health_is_unavailable_when_the_database_is_unreachable(
    create_app: AppFactory, service: str, unreachable_db: Engine
) -> None:
    client = TestClient(create_app(unreachable_db, ManualClock(CLOSE_OF_BUSINESS)))

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "service": service,
        "status": "unavailable",
        "database": "unreachable",
        "checked_at": "2026-03-02T21:00:00Z",
    }
