"""The Simulator as a Provider (SPEC §6): create a Payment with an Idempotency Key, get it,
settle ACH on the Banking Day calendar, and deliver signed Provider Events with retries and
backoff, over real HTTP, driven by the Clock."""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from railbench.common.clock import ManualClock
from railbench.common.settings import Settings
from railbench.common.signing import SIGNATURE_HEADER, verify
from railbench.simulator.app import create_app
from railbench.simulator.provider import Simulator
from tests.integration.helpers import unique
from tests.integration.servers import serving

pytestmark = pytest.mark.integration

SECRET = "simulator-test-secret"
# Thursday 3 September 2026, 15:00 in New York (EDT, UTC-4): before the 16:00 ACH Cutoff.
THURSDAY_AFTERNOON = datetime(2026, 9, 3, 19, 0, tzinfo=UTC)
FRIDAY_SETTLEMENT = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)  # 08:30 in New York
TUESDAY_SETTLEMENT = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)  # after Labor Day


@dataclass
class Receiver:
    """A stand-in webhook endpoint that answers with the queued statuses, then 200."""

    statuses: list[int] = field(default_factory=list)
    received: list[tuple[bytes, dict[str, str]]] = field(default_factory=list)

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/events")
        async def events(request: Request) -> Response:
            self.received.append((await request.body(), dict(request.headers)))
            return Response(status_code=self.statuses.pop(0) if self.statuses else 200)

        return app


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(THURSDAY_AFTERNOON)


@pytest.fixture
def receiver() -> Receiver:
    return Receiver()


@pytest.fixture
def client(engine: Engine, clock: ManualClock, receiver: Receiver) -> Iterator[TestClient]:
    with serving(receiver.app()) as url:
        settings = Settings(
            webhook_signing_secret=SECRET,
            ledger_webhook_url=f"{url}/events",
        )
        yield TestClient(create_app(engine, clock, settings))


def simulator(client: TestClient) -> Simulator:
    found: Simulator = client.app.state.simulator  # type: ignore[attr-defined]
    return found


def ach_pull(key: str = "", amount: int = 10_000, **changes: Any) -> dict[str, Any]:
    return {
        "idempotency_key": key or unique("pull"),
        "rail": "ach",
        "direction": "pull",
        "amount": amount,
        "currency": "USD",
        "reference": "Mintwharf buyer funding",
        **changes,
    }


def create(client: TestClient, request: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/payments", json=request)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def events_for(receiver: Receiver, payment_id: str) -> list[dict[str, Any]]:
    bodies = [json.loads(body) for body, _ in receiver.received]
    return [body for body in bodies if body["payment"]["id"] == payment_id]


def test_creating_an_ach_pull_submits_it(client: TestClient) -> None:
    payment = create(client, ach_pull(amount=10_000))

    assert payment["status"] == "submitted"
    assert payment["amount"] == 10_000
    assert payment["submitted_at"] == "2026-09-03T19:00:00Z"
    assert payment["settled_at"] is None
    assert client.get(f"/payments/{payment['id']}").json() == payment


def test_creating_is_idempotent(client: TestClient) -> None:
    request = ach_pull()
    first = create(client, request)

    again = client.post("/payments", json=request)
    different = client.post("/payments", json={**request, "amount": 10_001})

    assert again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json() == first
    assert different.status_code == 409


@pytest.mark.parametrize(
    "change",
    [{"amount": 12.5}, {"amount": "100"}, {"amount": 0}, {"rail": "rtp"}, {"direction": "push"}],
)
def test_a_payment_the_simulator_cannot_make_is_refused(
    client: TestClient, change: dict[str, Any]
) -> None:
    assert client.post("/payments", json=ach_pull(**change)).status_code == 422


def test_an_unknown_payment_is_not_found(client: TestClient) -> None:
    assert client.get("/payments/simpay_0").status_code == 404


def test_a_submitted_event_is_delivered_signed(
    client: TestClient, clock: ManualClock, receiver: Receiver
) -> None:
    payment = create(client, ach_pull())

    simulator(client).tick()

    [(body, headers)] = [
        (raw, headers)
        for raw, headers in receiver.received
        if json.loads(raw)["payment"]["id"] == payment["id"]
    ]
    verify(SECRET, body, headers[SIGNATURE_HEADER.lower()], now=clock.now())
    event = json.loads(body)
    assert event["type"] == "payment.submitted"
    assert event["event_id"].startswith("simevt_")
    assert event["occurred_at"] == "2026-09-03T19:00:00Z"
    assert event["payment"]["status"] == "submitted"


@pytest.mark.parametrize(
    ("created_at", "settles_at"),
    [
        pytest.param(THURSDAY_AFTERNOON, FRIDAY_SETTLEMENT, id="before Cutoff: next Banking Day"),
        pytest.param(
            datetime(2026, 9, 3, 21, 0, tzinfo=UTC),  # 17:00 in New York
            TUESDAY_SETTLEMENT,
            id="after Cutoff Thursday: counts as Friday, settles after Labor Day",
        ),
        pytest.param(
            datetime(2026, 9, 5, 15, 0, tzinfo=UTC),
            datetime(2026, 9, 9, 12, 30, tzinfo=UTC),
            id="on a Saturday: counts as Tuesday, settles Wednesday",
        ),
    ],
)
def test_an_ach_pull_settles_on_the_banking_day_after_submission(
    client: TestClient,
    clock: ManualClock,
    receiver: Receiver,
    created_at: datetime,
    settles_at: datetime,
) -> None:
    clock.set(created_at)
    payment = create(client, ach_pull())

    clock.set(settles_at - timedelta(seconds=1))
    simulator(client).tick()
    assert client.get(f"/payments/{payment['id']}").json()["status"] == "submitted"

    clock.set(settles_at)
    simulator(client).tick()

    settled = client.get(f"/payments/{payment['id']}").json()
    assert settled["status"] == "settled"
    assert settled["settled_at"] == settles_at.isoformat().replace("+00:00", "Z")
    assert [event["type"] for event in events_for(receiver, payment["id"])] == [
        "payment.submitted",
        "payment.settled",
    ]


def test_a_failed_delivery_is_retried_with_backoff(
    client: TestClient, clock: ManualClock, receiver: Receiver
) -> None:
    payment = create(client, ach_pull())
    receiver.statuses[:] = [500, 503]

    simulator(client).tick()  # attempt 1 fails
    simulator(client).tick()  # not due again yet
    clock.advance(timedelta(seconds=30))
    simulator(client).tick()  # attempt 2 fails; the next waits 60s
    clock.advance(timedelta(seconds=59))
    simulator(client).tick()
    clock.advance(timedelta(seconds=1))
    simulator(client).tick()  # attempt 3 succeeds
    clock.advance(timedelta(hours=1))
    simulator(client).tick()  # delivered: never sent again

    attempts = [
        (raw, headers)
        for raw, headers in receiver.received
        if json.loads(raw)["payment"]["id"] == payment["id"]
    ]
    assert len(attempts) == 3
    # Every attempt carries the same event and body, freshly signed.
    assert len({raw for raw, _ in attempts}) == 1
    assert len({headers[SIGNATURE_HEADER.lower()] for _, headers in attempts}) == 3


def test_an_unreachable_webhook_url_is_retried(
    engine: Engine, clock: ManualClock, receiver: Receiver
) -> None:
    settings = Settings(
        webhook_signing_secret=SECRET,
        ledger_webhook_url="http://127.0.0.1:9/events",  # nothing listens on the discard port
    )
    client = TestClient(create_app(engine, clock, settings))
    payment = create(client, ach_pull())

    simulator(client).tick()

    delivery = simulator(client).deliveries(payment["id"])
    assert [(d.attempts, d.delivered) for d in delivery] == [(1, False)]
    assert delivery[0].last_error != ""
