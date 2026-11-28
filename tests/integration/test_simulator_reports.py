"""The Simulator publishes one Settlement Report per Provider per Banking Day (SPEC §6, ADR
0014), empty ones included, frozen as published and served signed; report Faults go into
the Fault Manifest when the report they show on is published. Each test gets its own
database, since reports cover everything the Simulator ever settled."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from railbench.common.clock import ManualClock
from railbench.common.settings import Settings
from railbench.common.signing import SIGNATURE_HEADER, verify
from railbench.scenarios.runner import fresh_database
from railbench.simulator.app import create_app
from railbench.simulator.model import PaymentPlan
from railbench.simulator.provider import Simulator

pytestmark = pytest.mark.integration

SECRET = "report-secret"
THURSDAY_3PM = datetime(2026, 9, 3, 19, 0, tzinfo=UTC)
THURSDAY_REPORT = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)  # 20:00 New York
FRIDAY_REPORT = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
TUESDAY_REPORT = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
HEADER = (
    b"report_date,provider,line_id,type,provider_payment_id,idempotency_key,rail,direction,"
    b"currency,amount,settled_at,original_line_id,reason\n"
)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(THURSDAY_3PM)


@pytest.fixture
def client(database_url: str, clock: ManualClock) -> Iterator[TestClient]:
    with fresh_database(database_url, prefix="reports") as url:
        engine = create_engine(url)
        settings = Settings(webhook_signing_secret=SECRET, ledger_webhook_url="http://x.invalid")
        try:
            yield TestClient(create_app(engine, clock, settings))
        finally:
            engine.dispose()


def simulator(client: TestClient) -> Simulator:
    found: Simulator = client.app.state.simulator  # type: ignore[attr-defined]
    return found


def pull(client: TestClient, key: str, amount: int = 10_000) -> str:
    response = client.post(
        "/payments",
        json={
            "idempotency_key": key,
            "rail": "ach",
            "direction": "pull",
            "amount": amount,
            "currency": "USD",
        },
    )
    assert response.status_code == 201, response.text
    payment_id: str = response.json()["id"]
    return payment_id


def at(client: TestClient, clock: ManualClock, moment: datetime) -> None:
    clock.set(moment)
    sim = simulator(client)
    sim._settle_due()  # settle without delivering: nothing listens in these tests
    sim.publish_reports()


def report(client: TestClient, day: str) -> bytes:
    response = client.get(f"/settlement-reports/simulator/{day}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    body: bytes = response.content
    return body


def test_each_banking_day_gets_a_report_once_it_is_due(
    client: TestClient, clock: ManualClock
) -> None:
    payment_id = pull(client, "pull-1")
    assert simulator(client).next_report_due() is None  # nothing settled yet

    at(client, clock, THURSDAY_REPORT - timedelta(seconds=1))
    assert client.get("/settlement-reports/simulator/2026-09-03").status_code == 404

    at(client, clock, datetime(2026, 9, 4, 12, 30, tzinfo=UTC))  # Friday's settlement
    assert report(client, "2026-09-03") == HEADER  # caught up: nothing settled Thursday
    assert simulator(client).next_report_due() == FRIDAY_REPORT
    at(client, clock, TUESDAY_REPORT)

    assert report(client, "2026-09-03") == HEADER  # nothing settled Thursday
    friday = report(client, "2026-09-04")
    assert friday.startswith(HEADER)
    assert f",settlement,{payment_id},pull-1,ach,pull,USD,10000,".encode() in friday
    assert report(client, "2026-09-08") == HEADER  # after Labor Day
    for weekend_or_holiday in ("2026-09-05", "2026-09-06", "2026-09-07"):
        assert client.get(f"/settlement-reports/simulator/{weekend_or_holiday}").status_code == 404
    assert client.get("/settlement-reports/elsewhere/2026-09-04").status_code == 404
    assert simulator(client).next_report_due() is None


def test_a_report_is_signed_like_a_webhook_and_frozen_as_published(
    client: TestClient, clock: ManualClock
) -> None:
    pull(client, "pull-1")
    at(client, clock, FRIDAY_REPORT)
    response = client.get("/settlement-reports/simulator/2026-09-04")

    verify(SECRET, response.content, response.headers[SIGNATURE_HEADER], clock.now())
    at(client, clock, FRIDAY_REPORT + timedelta(days=3))
    assert report(client, "2026-09-04") == response.content


def test_report_faults_are_in_the_manifest_once_their_report_is_published(
    client: TestClient, clock: ManualClock
) -> None:
    sim = simulator(client)
    for key, faults in (
        ("gone", [{"id": "gone", "kind": "missing_report_line"}]),
        ("off", [{"id": "off", "kind": "wrong_report_amount", "by": 5}]),
        ("late", [{"id": "late", "kind": "settlement_after_cutoff"}]),
        (
            "fee",
            [
                {
                    "id": "fee",
                    "kind": "provider_adjustment",
                    "amount": 99,
                    "reason": "fee_correction",
                }
            ],
        ),
    ):
        sim.plan(PaymentPlan.model_validate({"idempotency_key": key, "faults": faults}))
        pull(client, key)

    at(client, clock, FRIDAY_REPORT)
    assert {(f.fault_id, f.cause_code) for f in sim.faults()} == {
        ("off", "wrong_report_amount"),
        ("gone", "missing_report_line"),
    }
    assert sim.next_report_due() == TUESDAY_REPORT

    at(client, clock, TUESDAY_REPORT)
    found = {f.fault_id: f for f in sim.faults()}
    assert {k: (f.kind, f.event_type) for k, f in found.items()} == {
        "gone": ("missing_report_line", "settlement_report.line"),
        "off": ("wrong_report_amount", "settlement_report.line"),
        "late": ("settlement_after_cutoff", "settlement_report.line"),
        "fee": ("provider_adjustment", "settlement_report.adjustment"),
    }
    assert found["fee"].injected_at == TUESDAY_REPORT
    assert found["fee"].event_id.encode() in report(client, "2026-09-08")
    assert found["late"].event_id.encode() in report(client, "2026-09-08")
    assert found["gone"].event_id.encode() not in report(client, "2026-09-04")
    assert b",USD,10005," in report(client, "2026-09-04")
    assert sim.next_report_due() is None


@pytest.mark.parametrize(
    "fault",
    [
        {"id": "x", "kind": "wrong_report_amount", "by": -10_000},
        {"id": "x", "kind": "provider_adjustment", "amount": 10_001, "reason": "fee_correction"},
        {"id": "x", "kind": "provider_adjustment", "amount": 1, "reason": "payout_recall"},
    ],
)
def test_a_report_fault_the_payment_cannot_take_is_refused(
    client: TestClient, fault: dict[str, Any]
) -> None:
    simulator(client).plan(PaymentPlan.model_validate({"idempotency_key": "k", "faults": [fault]}))

    response = client.post(
        "/payments",
        json={
            "idempotency_key": "k",
            "rail": "ach",
            "direction": "pull",
            "amount": 10_000,
            "currency": "USD",
        },
    )

    assert response.status_code == 409


def test_the_first_report_is_for_the_first_banking_day_anything_was_created(
    client: TestClient, clock: ManualClock
) -> None:
    clock.set(datetime(2026, 9, 5, 15, 0, tzinfo=UTC))  # a Saturday
    pull(client, "weekend")
    at(client, clock, datetime(2026, 9, 9, 1, 0, tzinfo=UTC))

    assert report(client, "2026-09-08") == HEADER  # submitted for Tuesday, settles Wednesday
    assert client.get("/settlement-reports/simulator/2026-09-04").status_code == 404
