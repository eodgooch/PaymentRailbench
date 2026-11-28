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
from railbench.scenarios.servers import serving
from railbench.simulator.app import create_app
from railbench.simulator.model import EventType, PaymentPlan
from railbench.simulator.provider import PlanConflict, Simulator, event_id_for
from tests.integration.helpers import unique

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


# Scripted outcomes and delivery Faults (SPEC §6, ADR 0011)


def planned(client: TestClient, outcome: str = "settle", *faults: dict[str, Any]) -> str:
    """Plan a Payment at the Simulator and create it; returns its Simulator ID."""
    key = unique("scripted")
    simulator(client).plan(
        PaymentPlan.model_validate(
            {"idempotency_key": key, "outcome": outcome, "faults": list(faults)}
        )
    )
    found: str = create(client, ach_pull(key))["id"]
    return found


def received(receiver: Receiver, payment_id: str) -> list[tuple[str, str]]:
    return [(event["type"], event["event_id"]) for event in events_for(receiver, payment_id)]


@pytest.mark.parametrize(
    ("outcome", "status", "event"),
    [("decline", "declined", "payment.declined"), ("cancel", "canceled", "payment.canceled")],
)
def test_a_planned_decline_or_cancellation_ends_the_payment_instead_of_settling(
    client: TestClient,
    clock: ManualClock,
    receiver: Receiver,
    outcome: str,
    status: str,
    event: str,
) -> None:
    payment_id = planned(client, outcome)

    clock.set(FRIDAY_SETTLEMENT)
    simulator(client).tick()

    found = client.get(f"/payments/{payment_id}").json()
    assert (found["status"], found["settled_at"]) == (status, None)
    assert found["ended_at"] == "2026-09-04T12:30:00Z"
    assert [type_ for type_, _ in received(receiver, payment_id)] == ["payment.submitted", event]


def test_an_event_id_is_derived_from_the_idempotency_key_and_the_event_type(
    client: TestClient, receiver: Receiver
) -> None:
    payment = create(client, ach_pull("pull-with-a-known-key-" + unique("k")))
    simulator(client).tick()

    [(_, event_id)] = received(receiver, payment["id"])
    assert event_id == event_id_for(payment["idempotency_key"], EventType.SUBMITTED)
    assert event_id_for("a", EventType.SUBMITTED) != event_id_for("a", EventType.SETTLED)
    assert event_id_for("a", EventType.SUBMITTED) == event_id_for("a", EventType.SUBMITTED)


def test_a_duplicate_fault_delivers_the_same_event_again(
    client: TestClient, receiver: Receiver
) -> None:
    payment_id = planned(
        client, "settle", {"id": "dup", "kind": "duplicate", "event": "submitted", "times": 2}
    )
    simulator(client).tick()

    bodies = [raw for raw, _ in receiver.received if json.loads(raw)["payment"]["id"] == payment_id]
    assert len(bodies) == 3
    assert len(set(bodies)) == 1  # the same event ID and the same bytes


def test_a_delay_fault_holds_the_event_back_on_the_clock(
    client: TestClient, clock: ManualClock, receiver: Receiver
) -> None:
    payment_id = planned(
        client, "settle", {"id": "late", "kind": "delay", "event": "submitted", "by": "PT2H"}
    )

    simulator(client).tick()
    clock.advance(timedelta(hours=2) - timedelta(seconds=1))
    simulator(client).tick()
    assert received(receiver, payment_id) == []
    assert simulator(client).next_due() == THURSDAY_AFTERNOON + timedelta(hours=2)

    clock.advance(timedelta(seconds=1))
    simulator(client).tick()
    assert [type_ for type_, _ in received(receiver, payment_id)] == ["payment.submitted"]


def test_an_out_of_order_fault_delivers_the_later_event_first(
    client: TestClient, clock: ManualClock, receiver: Receiver
) -> None:
    payment_id = planned(
        client,
        "settle",
        {"id": "swap", "kind": "out_of_order", "event": "submitted", "after": "settled"},
    )

    simulator(client).tick()
    assert received(receiver, payment_id) == []
    # Held, so it isn't due: the next thing due is the settlement.
    assert simulator(client).next_due() == FRIDAY_SETTLEMENT

    clock.set(FRIDAY_SETTLEMENT)
    simulator(client).tick()
    simulator(client).tick()
    assert [type_ for type_, _ in received(receiver, payment_id)] == [
        "payment.settled",
        "payment.submitted",
    ]


def test_every_injected_fault_is_in_the_manifest_with_its_root_cause(
    client: TestClient, clock: ManualClock
) -> None:
    payment_id = planned(
        client,
        "decline",
        {"id": "dup", "kind": "duplicate", "event": "declined"},
        {"id": "swap", "kind": "out_of_order", "event": "submitted", "after": "declined"},
    )
    clock.set(FRIDAY_SETTLEMENT)
    simulator(client).tick()

    manifest = [f for f in simulator(client).faults() if f.payment_id == payment_id]
    assert [(f.fault_id, f.kind, f.event_type, f.cause_code) for f in manifest] == [
        ("swap", "out_of_order", "payment.submitted", "out_of_order_delivery"),
        ("dup", "duplicate", "payment.declined", "duplicate_delivery"),
    ]
    swap, dup = manifest
    assert swap.injected_at == THURSDAY_AFTERNOON
    assert dup.injected_at == FRIDAY_SETTLEMENT
    assert swap.event_id in swap.root_cause and "payment.declined" in swap.root_cause
    assert dup.params == {"times": 1}


def test_a_plan_cannot_fault_an_event_its_payment_never_emits() -> None:
    with pytest.raises(ValueError, match="never emits"):
        PaymentPlan.model_validate(
            {
                "idempotency_key": "k",
                "outcome": "decline",
                "faults": [{"id": "d", "kind": "duplicate", "event": "settled"}],
            }
        )
    with pytest.raises(ValueError, match="not emitted after"):
        PaymentPlan.model_validate(
            {
                "idempotency_key": "k",
                "faults": [
                    {"id": "o", "kind": "out_of_order", "event": "settled", "after": "submitted"}
                ],
            }
        )


def test_a_payment_is_planned_once(client: TestClient) -> None:
    plan = PaymentPlan(idempotency_key=unique("once"), outcome="decline")
    simulator(client).plan(plan)
    simulator(client).plan(plan)  # the same plan again is fine

    with pytest.raises(PlanConflict):
        simulator(client).plan(plan.model_copy(update={"outcome": "cancel"}))
