"""The Inbox (SPEC §4.2): every Provider Event is verified, stored raw and deduplicated by
event ID before it's applied; applying moves the state machine forward only, records a
stale event and does nothing, and turns an impossible one into an exception record with no
Ledger write. Webhooks here are signed by hand, the way the Simulator signs them."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import Any, NoReturn
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from railbench.common.clock import ManualClock
from railbench.common.settings import Settings
from railbench.common.signing import SIGNATURE_HEADER, sign
from railbench.ledger.app import create_app
from railbench.ledger.journal import Journal, Rejected
from railbench.ledger.model import AccountType, NewAccount, NewEntry, NewTransaction, SystemJob
from railbench.ledger.tables import accounts, entries, transactions
from railbench.payments.model import NewPayment
from railbench.payments.provider import SimulatorProvider
from railbench.payments.service import Payments, clearing_account
from railbench.payments.tables import inbox
from tests.integration.helpers import unique

pytestmark = pytest.mark.integration

SECRET = "inbox-test-secret"
SUBMITTED_AT = datetime(2026, 9, 3, 19, 0, tzinfo=UTC)
SETTLED_AT = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)
AMOUNT = 7_500
CLEARING = clearing_account("simulator", "ach", "USD")


def fake_simulator(request: httpx.Request) -> httpx.Response:
    """The Simulator's create-Payment answer, without the Simulator."""
    body = json.loads(request.content)
    return httpx.Response(
        201, json={"id": f"simpay_{body['idempotency_key']}", "status": "submitted"}
    )


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(SUBMITTED_AT)


@pytest.fixture
def client(app_engine: Engine, clock: ManualClock) -> TestClient:
    provider = SimulatorProvider(
        "http://simulator.invalid",
        SECRET,
        http=httpx.Client(
            base_url="http://simulator.invalid", transport=httpx.MockTransport(fake_simulator)
        ),
    )
    return TestClient(create_app(app_engine, clock, Settings(), provider=provider))


def payments(client: TestClient) -> Payments:
    found: Payments = client.app.state.payments  # type: ignore[attr-defined]
    return found


def journal(client: TestClient) -> Journal:
    found: Journal = client.app.state.journal  # type: ignore[attr-defined]
    return found


@pytest.fixture
def payment(client: TestClient) -> dict[str, Any]:
    customer = unique("customer")
    journal(client).open_account(
        NewAccount(code=customer, type=AccountType.LIABILITY, currency="USD")
    )
    response = client.post(
        "/payments",
        json={
            "idempotency_key": unique("pull"),
            "rail": "ach",
            "direction": "pull",
            "customer_account": customer,
            "amount": AMOUNT,
            "currency": "USD",
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def event_body(payment: dict[str, Any], type_: str, occurred_at: datetime, event_id: str) -> bytes:
    return json.dumps(
        {
            "event_id": event_id,
            "type": type_,
            "occurred_at": occurred_at.isoformat(),
            "payment": {"id": payment["provider_payment_id"]},
        }
    ).encode()


def deliver(
    client: TestClient,
    clock: ManualClock,
    payment: dict[str, Any],
    type_: str,
    occurred_at: datetime = SUBMITTED_AT,
    event_id: str = "",
    secret: str = SECRET,
) -> httpx.Response:
    body = event_body(payment, type_, occurred_at, event_id or f"evt_{uuid4().hex}")
    response: httpx.Response = client.post(
        "/providers/simulator/events",
        content=body,
        headers={SIGNATURE_HEADER: sign(secret, body, clock.now())},
    )
    return response


def customer_balances(client: TestClient, payment: dict[str, Any]) -> tuple[int, int, int]:
    found = journal(client).balances(payment["customer_account"], datetime(9999, 1, 1, tzinfo=UTC))
    return found.posted, found.pending, found.available


def stored(engine: Engine, event_id: str) -> int:
    with engine.connect() as connection:
        count: int = connection.execute(
            select(func.count()).select_from(inbox).where(inbox.c.event_id == event_id)
        ).scalar_one()
    return count


def test_a_submitted_event_books_the_pull_as_pending(
    client: TestClient, clock: ManualClock, payment: dict[str, Any]
) -> None:
    response = deliver(client, clock, payment, "payment.submitted")

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "applied"
    assert response.json()["duplicate"] is False
    assert customer_balances(client, payment) == (0, AMOUNT, 0)


def test_an_event_is_stored_raw(
    client: TestClient, clock: ManualClock, payment: dict[str, Any], engine: Engine
) -> None:
    body = event_body(payment, "payment.submitted", SUBMITTED_AT, "evt_raw_" + uuid4().hex)
    signature = sign(SECRET, body, clock.now())

    client.post("/providers/simulator/events", content=body, headers={SIGNATURE_HEADER: signature})

    with engine.connect() as connection:
        row = connection.execute(select(inbox).where(inbox.c.body == body)).one()
    assert row.headers[SIGNATURE_HEADER.lower()] == signature
    assert row.received_at == SUBMITTED_AT


@pytest.mark.parametrize("secret", ["not-the-secret"])
def test_a_badly_signed_event_is_refused_and_not_stored(
    client: TestClient, clock: ManualClock, payment: dict[str, Any], engine: Engine, secret: str
) -> None:
    event_id = f"evt_forged_{uuid4().hex}"

    response = deliver(
        client, clock, payment, "payment.submitted", event_id=event_id, secret=secret
    )
    unsigned = client.post(
        "/providers/simulator/events",
        content=event_body(payment, "payment.submitted", SUBMITTED_AT, event_id),
    )

    assert (response.status_code, unsigned.status_code) == (401, 401)
    assert stored(engine, event_id) == 0
    assert customer_balances(client, payment) == (0, 0, 0)


def test_a_signed_body_that_is_not_an_event_is_refused(
    client: TestClient, clock: ManualClock
) -> None:
    body = b'{"type": "payment.exploded"}'

    response = client.post(
        "/providers/simulator/events",
        content=body,
        headers={SIGNATURE_HEADER: sign(SECRET, body, clock.now())},
    )

    assert response.status_code == 400


def test_an_event_for_a_payment_not_yet_recorded_asks_for_a_retry(
    client: TestClient, clock: ManualClock
) -> None:
    response = deliver(client, clock, {"provider_payment_id": "simpay_unknown"}, "payment.settled")

    assert response.status_code == 503


def test_a_redelivered_event_is_deduplicated_before_it_is_applied(
    client: TestClient, clock: ManualClock, payment: dict[str, Any], engine: Engine
) -> None:
    event_id = f"evt_{uuid4().hex}"

    first = deliver(client, clock, payment, "payment.submitted", event_id=event_id)
    again = deliver(client, clock, payment, "payment.submitted", event_id=event_id)

    assert again.status_code == 200
    assert again.json() == {**first.json(), "duplicate": True}
    assert stored(engine, event_id) == 1
    assert customer_balances(client, payment) == (0, AMOUNT, 0)


def race[T](attempt: Callable[[], T], racers: int = 8) -> list[T]:
    start = Barrier(racers)

    def run() -> T:
        start.wait()
        return attempt()

    with ThreadPoolExecutor(racers) as pool:
        return [future.result() for future in [pool.submit(run) for _ in range(racers)]]


def test_concurrent_duplicate_deliveries_apply_once(
    client: TestClient, clock: ManualClock, payment: dict[str, Any], engine: Engine
) -> None:
    deliver(client, clock, payment, "payment.submitted")
    event_id = f"evt_{uuid4().hex}"
    body = event_body(payment, "payment.settled", SETTLED_AT, event_id)
    clock.set(SETTLED_AT)
    headers = {SIGNATURE_HEADER: sign(SECRET, body, clock.now())}

    receipts = race(lambda: payments(client).receive("simulator", body, headers))

    assert stored(engine, event_id) == 1
    assert {receipt.outcome for receipt in receipts} == {"applied"}
    assert sorted(receipt.duplicate for receipt in receipts) == [False] + [True] * 7
    assert customer_balances(client, payment) == (AMOUNT, 0, 0)
    found = payments(client).payment(payment["id"])
    assert [t.state for t in found.transitions] == ["created", "submitted", "settled"]


def test_an_event_for_a_state_already_passed_is_recorded_and_does_nothing(
    client: TestClient, clock: ManualClock, payment: dict[str, Any]
) -> None:
    deliver(client, clock, payment, "payment.submitted")
    clock.set(SETTLED_AT)
    deliver(client, clock, payment, "payment.settled", SETTLED_AT)
    before = customer_balances(client, payment)

    # A second Provider Event, with its own ID, reporting a state already passed.
    response = deliver(client, clock, payment, "payment.submitted")

    assert response.json()["outcome"] == "stale"
    assert customer_balances(client, payment) == before
    assert payments(client).payment(payment["id"]).state == "settled"


def test_an_impossible_transition_is_an_exception_with_no_ledger_write(
    client: TestClient, clock: ManualClock, payment: dict[str, Any]
) -> None:
    deliver(client, clock, payment, "payment.submitted")
    clock.set(SETTLED_AT)
    deliver(client, clock, payment, "payment.settled", SETTLED_AT)
    before = customer_balances(client, payment)
    clearing_before = journal(client).balances(CLEARING, SETTLED_AT)

    response = deliver(client, clock, payment, "payment.declined", SETTLED_AT)

    assert response.status_code == 200
    assert response.json()["outcome"] == "exception"
    assert "settled to declined" in response.json()["detail"]
    assert customer_balances(client, payment) == before
    assert journal(client).balances(CLEARING, SETTLED_AT) == clearing_before
    found = payments(client).payment(payment["id"])
    assert found.state == "settled"
    assert [e.inbox_id for e in found.exceptions] == [response.json()["inbox_id"]]


@pytest.mark.parametrize("type_", ["payment.declined", "payment.canceled"])
def test_a_pull_declined_before_settlement_cancels_its_pending_transaction(
    client: TestClient, clock: ManualClock, payment: dict[str, Any], type_: str
) -> None:
    deliver(client, clock, payment, "payment.submitted")

    response = deliver(client, clock, payment, type_)

    assert response.json()["outcome"] == "applied"
    assert customer_balances(client, payment) == (0, 0, 0)
    found = payments(client).payment(payment["id"])
    assert found.state == type_.removeprefix("payment.")
    reversal = journal(client).transaction(found.transitions[-1].transaction_id or 0)
    assert reversal.reverses == found.transitions[1].transaction_id
    # And nothing can settle it now.
    clock.set(SETTLED_AT)
    late = deliver(client, clock, payment, "payment.settled", SETTLED_AT)
    assert late.json()["outcome"] == "exception"
    assert customer_balances(client, payment) == (0, 0, 0)


def test_a_pull_canceled_before_submission_books_nothing(
    client: TestClient, clock: ManualClock, payment: dict[str, Any]
) -> None:
    response = deliver(client, clock, payment, "payment.canceled")

    assert response.json()["outcome"] == "applied"
    found = payments(client).payment(payment["id"])
    assert (found.state, found.transitions[-1].transaction_id) == ("canceled", None)
    assert customer_balances(client, payment) == (0, 0, 0)


def test_a_settled_event_for_a_payment_never_reported_submitted_books_both_steps(
    client: TestClient, clock: ManualClock, payment: dict[str, Any]
) -> None:
    clock.set(SETTLED_AT)

    response = deliver(client, clock, payment, "payment.settled", SETTLED_AT)

    assert response.json()["outcome"] == "applied"
    found = payments(client).payment(payment["id"])
    assert [t.state for t in found.transitions] == ["created", "submitted", "settled"]
    assert customer_balances(client, payment) == (AMOUNT, 0, 0)


def test_the_payments_api_is_idempotent(client: TestClient, payment: dict[str, Any]) -> None:
    request = {
        "idempotency_key": payment["idempotency_key"],
        "rail": "ach",
        "direction": "pull",
        "customer_account": payment["customer_account"],
        "amount": AMOUNT,
        "currency": "USD",
    }

    again = client.post("/payments", json=request)
    different = client.post("/payments", json={**request, "amount": AMOUNT + 1})

    assert again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["id"] == payment["id"]
    assert different.status_code == 409


@pytest.mark.parametrize(
    ("account_type", "amount"),
    [(AccountType.ASSET, 100), (AccountType.LIABILITY, 12.5), (AccountType.LIABILITY, 0)],
)
def test_a_pull_needs_a_customer_balance_and_a_whole_positive_amount(
    client: TestClient, account_type: AccountType, amount: float
) -> None:
    code = unique("account")
    journal(client).open_account(NewAccount(code=code, type=account_type, currency="USD"))

    response = client.post(
        "/payments",
        json={
            "idempotency_key": unique("pull"),
            "rail": "ach",
            "direction": "pull",
            "customer_account": code,
            "amount": amount,
            "currency": "USD",
        },
    )

    assert response.status_code == 422


def test_a_refused_step_rolls_back_every_ledger_write_of_the_event(
    client: TestClient,
    clock: ManualClock,
    payment: dict[str, Any],
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise Rejected("injected refusal")

    # A settled event for a Payment still created books two steps: the pending
    # Transaction (which succeeds), then its Settlement (refused here).
    monkeypatch.setattr(Journal, "settle", refuse)
    clock.set(SETTLED_AT)

    response = deliver(client, clock, payment, "payment.settled", SETTLED_AT)

    assert response.json()["outcome"] == "exception"
    assert "injected refusal" in response.json()["detail"]
    with engine.connect() as connection:
        booked = connection.execute(
            select(func.count())
            .select_from(transactions)
            .where(transactions.c.idempotency_key.startswith(f"payment:{payment['id']}:"))
        ).scalar_one()
    assert booked == 0
    assert customer_balances(client, payment) == (0, 0, 0)
    found = payments(client).payment(payment["id"])
    assert (found.state, [t.state for t in found.transitions]) == ("created", ["created"])
    assert [e.inbox_id for e in found.exceptions] == [response.json()["inbox_id"]]


# Pushes: Payouts leave a Customer Balance through the Clearing Account to Bank Cash


def funded_customer(client: TestClient, amount: int) -> str:
    """A Customer Balance holding `amount`, Posted and Available."""
    customer = unique("seller")
    journal(client).open_account(
        NewAccount(code=customer, type=AccountType.LIABILITY, currency="USD")
    )
    source = unique("funding")
    journal(client).open_account(NewAccount(code=source, type=AccountType.ASSET, currency="USD"))
    journal(client).post(
        NewTransaction(
            idempotency_key=unique("fund"),
            actor=SystemJob(name="test", version="1"),
            effective_at=SUBMITTED_AT,
            entries=[
                NewEntry(account=source, currency="USD", amount=amount),
                NewEntry(account=customer, currency="USD", amount=-amount),
            ],
        )
    )
    return customer


def push(client: TestClient, rail: str, customer: str, amount: int = AMOUNT) -> dict[str, Any]:
    created = payments(client).originate(
        NewPayment.model_validate(
            {
                "idempotency_key": unique("payout"),
                "rail": rail,
                "direction": "push",
                "customer_account": customer,
                "amount": amount,
                "currency": "USD",
            }
        )
    )
    return created.payment.model_dump(mode="json")


def booked_on(engine: Engine, payment: dict[str, Any], account: str) -> int:
    """What this Payment's own Transactions add up to on `account` (shared Accounts)."""
    with engine.connect() as connection:
        total = connection.execute(
            select(func.coalesce(func.sum(entries.c.amount), 0))
            .join(transactions, transactions.c.id == entries.c.transaction_id)
            .join(accounts, accounts.c.id == entries.c.account_id)
            .where(
                transactions.c.idempotency_key.startswith(f"payment:{payment['id']}:"),
                accounts.c.code == account,
            )
        ).scalar_one()
    return int(total)


def test_a_settled_rtp_push_pays_out_through_the_rtp_clearing_account(
    client: TestClient, clock: ManualClock, engine: Engine
) -> None:
    customer = funded_customer(client, 10_000)
    payout = push(client, "rtp", customer, 7_500)
    assert payout["state"] == "created"

    response = deliver(client, clock, payout, "payment.settled")

    assert response.json()["outcome"] == "applied"
    assert customer_balances(client, payout) == (2_500, 0, 2_500)
    rtp_clearing = clearing_account("simulator", "rtp", "USD")
    assert booked_on(engine, payout, rtp_clearing) == 0  # nets to zero at once
    assert booked_on(engine, payout, "bank-cash:usd") == -7_500
    found = payments(client).payment(payout["id"])
    assert [t.state for t in found.transitions] == ["created", "settled"]
    assert found.available_at is None


def test_a_declined_rtp_push_books_nothing(
    client: TestClient, clock: ManualClock, engine: Engine
) -> None:
    customer = funded_customer(client, 10_000)
    payout = push(client, "rtp", customer)

    response = deliver(client, clock, payout, "payment.declined")

    assert response.json()["outcome"] == "applied"
    assert customer_balances(client, payout) == (10_000, 0, 10_000)
    assert booked_on(engine, payout, "bank-cash:usd") == 0


@pytest.mark.parametrize("type_", ["payment.submitted", "payment.canceled"])
def test_an_rtp_push_cannot_be_submitted_or_canceled(
    client: TestClient, clock: ManualClock, type_: str
) -> None:
    customer = funded_customer(client, 10_000)
    payout = push(client, "rtp", customer)

    response = deliver(client, clock, payout, type_)

    assert response.json()["outcome"] == "exception"
    assert customer_balances(client, payout) == (10_000, 0, 10_000)


def test_a_settled_rtp_push_is_final(client: TestClient, clock: ManualClock) -> None:
    customer = funded_customer(client, 10_000)
    payout = push(client, "rtp", customer)
    deliver(client, clock, payout, "payment.settled")

    response = deliver(client, clock, payout, "payment.declined")

    assert response.json()["outcome"] == "exception"
    assert customer_balances(client, payout) == (2_500, 0, 2_500)


def test_an_ach_push_is_an_unsettled_outflow_until_it_settles(
    client: TestClient, clock: ManualClock, engine: Engine
) -> None:
    customer = funded_customer(client, 10_000)
    payout = push(client, "ach", customer, 7_500)
    ach_clearing = clearing_account("simulator", "ach", "USD")

    deliver(client, clock, payout, "payment.submitted")
    # Still Posted (the money hasn't left), but no longer Available to anything else.
    assert customer_balances(client, payout) == (10_000, -7_500, 2_500)
    assert booked_on(engine, payout, ach_clearing) == -7_500

    clock.set(SETTLED_AT)
    deliver(client, clock, payout, "payment.settled", SETTLED_AT)
    assert customer_balances(client, payout) == (2_500, 0, 2_500)
    assert booked_on(engine, payout, ach_clearing) == 0
    assert booked_on(engine, payout, "bank-cash:usd") == -7_500

    # A push is never made Available: that's for money coming in.
    clock.set(datetime(2026, 9, 30, tzinfo=UTC))
    payments(client).make_available()
    assert payments(client).payment(payout["id"]).state == "settled"


def test_a_declined_ach_push_gives_the_money_back(
    client: TestClient, clock: ManualClock, engine: Engine
) -> None:
    customer = funded_customer(client, 10_000)
    payout = push(client, "ach", customer, 7_500)
    deliver(client, clock, payout, "payment.submitted")

    clock.set(SETTLED_AT)
    deliver(client, clock, payout, "payment.declined", SETTLED_AT)

    assert customer_balances(client, payout) == (10_000, 0, 10_000)
    assert booked_on(engine, payout, clearing_account("simulator", "ach", "USD")) == 0


@pytest.mark.parametrize(("rail", "direction"), [("ach", "push"), ("rtp", "push"), ("rtp", "pull")])
def test_the_payments_api_only_originates_pulls(
    client: TestClient, rail: str, direction: str
) -> None:
    """Payouts are made by an Auction Close, never by a bare API call."""
    customer = funded_customer(client, 10_000)

    response = client.post(
        "/payments",
        json={
            "idempotency_key": unique("push"),
            "rail": rail,
            "direction": direction,
            "customer_account": customer,
            "amount": 100,
            "currency": "USD",
        },
    )

    assert response.status_code == 422
