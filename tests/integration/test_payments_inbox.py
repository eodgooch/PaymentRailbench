"""The Inbox (SPEC §4.2): every Provider Event is verified, stored raw and deduplicated by
event ID before it's applied; applying moves the state machine forward only, records a
stale event and does nothing, and turns an impossible one into an exception record with no
Ledger write. Webhooks here are signed by hand, the way the Simulator signs them."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
from railbench.ledger.journal import AccountNotFound, Journal, Rejected
from railbench.ledger.model import (
    AccountType,
    NewAccount,
    NewEntry,
    NewHold,
    NewTransaction,
    SystemJob,
)
from railbench.ledger.tables import accounts, entries, transactions
from railbench.payments.model import NewPayment
from railbench.payments.provider import SimulatorProvider
from railbench.payments.service import HoldMismatch, Payments, clearing_account
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


def test_a_push_can_only_name_a_hold_reserving_its_own_amount(client: TestClient) -> None:
    customer = funded_customer(client, 10_000)
    hold = (
        journal(client)
        .place_hold(
            NewHold(
                idempotency_key=unique("reservation"),
                actor=SystemJob(name="test", version="1"),
                effective_at=SUBMITTED_AT,
                account=customer,
                currency="USD",
                amount=5_000,
            )
        )
        .hold
    )

    with pytest.raises(HoldMismatch):
        payments(client).originate(
            NewPayment(
                idempotency_key=unique("payout"),
                rail="rtp",
                direction="push",
                customer_account=customer,
                amount=7_500,
                currency="USD",
                hold_id=hold.id,
            )
        )


# Returns (SPEC §4.1, §4.3, ADR 0009, ADR 0013): a Return is a posted Reversal of the funding,
# never an edit; a deficit it leaves moves to Recovery Receivable, with an exception.
AVAILABLE_AT = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)  # midnight New York, after Labor Day
MONDAY = datetime(2026, 9, 7, 15, 0, tzinfo=UTC)  # Labor Day: inside the R01 window
OCTOBER = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)  # a Late Return, inside the R10 window
BANK_CASH = "bank-cash:usd"


def deliver_return(
    client: TestClient,
    clock: ManualClock,
    payment: dict[str, Any],
    occurred_at: datetime,
    code: str = "R10",
    event_id: str = "",
) -> httpx.Response:
    body = json.dumps(
        {
            "event_id": event_id or f"evt_{uuid4().hex}",
            "type": "payment.returned",
            "occurred_at": occurred_at.isoformat(),
            "payment": {"id": payment["provider_payment_id"]},
            "return": {"code": code, "reason": "a reason"},
        }
    ).encode()
    response: httpx.Response = client.post(
        "/providers/simulator/events",
        content=body,
        headers={SIGNATURE_HEADER: sign(SECRET, body, clock.now())},
    )
    return response


def settled(client: TestClient, clock: ManualClock, payment: dict[str, Any]) -> None:
    deliver(client, clock, payment, "payment.submitted")
    clock.set(SETTLED_AT)
    deliver(client, clock, payment, "payment.settled", SETTLED_AT)


def made_available(client: TestClient, clock: ManualClock, payment: dict[str, Any]) -> None:
    settled(client, clock, payment)
    clock.set(AVAILABLE_AT)
    payments(client).make_available()
    assert customer_balances(client, payment) == (AMOUNT, 0, AMOUNT)


def spend(client: TestClient, customer: str, amount: int, at: datetime) -> None:
    """The Customer pays for something (a stand-in for a lot bought at an Auction Close)."""
    seller = unique("seller")
    journal(client).open_account(
        NewAccount(code=seller, type=AccountType.LIABILITY, currency="USD")
    )
    journal(client).post(
        NewTransaction(
            idempotency_key=unique("purchase"),
            actor=SystemJob(name="test", version="1"),
            effective_at=at,
            entries=[
                NewEntry(account=customer, currency="USD", amount=amount),
                NewEntry(account=seller, currency="USD", amount=-amount),
            ],
        )
    )


def recovery_receivable(client: TestClient, payment: dict[str, Any]) -> int:
    code = f"recovery-receivable:{payment['customer_account']}"
    try:
        return journal(client).balances(code, datetime(9999, 1, 1, tzinfo=UTC)).posted
    except AccountNotFound:  # opened only for a Customer who ends up owing
        return 0


def test_a_return_before_the_funds_are_available_reverses_the_funding_and_its_hold(
    client: TestClient, clock: ManualClock, payment: dict[str, Any], engine: Engine
) -> None:
    settled(client, clock, payment)
    clock.set(MONDAY)

    response = deliver_return(client, clock, payment, MONDAY, code="R01")

    assert response.json()["outcome"] == "applied", response.text
    found = payments(client).payment(payment["id"])
    assert [t.state for t in found.transitions] == ["created", "submitted", "settled", "returned"]
    # A posted Reversal of the funding, effective when the Return happened; nothing edited.
    funding = journal(client).transaction(found.transitions[1].transaction_id or 0)
    reversal = journal(client).transaction(found.transitions[3].transaction_id or 0)
    assert (reversal.reverses, reversal.status) == (funding.id, "posted")
    assert reversal.entries[0].effective_at == MONDAY
    assert funding.settlement is not None  # still settled, as it was
    # The money left Bank Cash again, and the Clearing Account still nets to zero.
    assert booked_on(engine, payment, BANK_CASH) == 0
    assert booked_on(engine, payment, CLEARING) == 0
    # The availability Hold ends with the Return, so it never makes the money Available.
    hold = journal(client).hold(found.transitions[2].hold_id or 0)
    assert hold.ended is not None and hold.ended.effective_at == MONDAY
    assert customer_balances(client, payment) == (0, 0, 0)
    assert found.return_exceptions == []
    clock.set(AVAILABLE_AT)
    assert payments(client).make_available() == 0
    assert payments(client).payment(payment["id"]).state == "returned"


def test_a_late_return_with_the_money_still_there_leaves_no_deficit(
    client: TestClient, clock: ManualClock, payment: dict[str, Any]
) -> None:
    made_available(client, clock, payment)
    clock.set(OCTOBER)

    response = deliver_return(client, clock, payment, OCTOBER)

    assert response.json()["outcome"] == "applied"
    assert customer_balances(client, payment) == (0, 0, 0)
    assert recovery_receivable(client, payment) == 0
    assert payments(client).payment(payment["id"]).return_exceptions == []


def test_a_late_return_after_the_money_was_spent_moves_the_deficit_to_recovery_receivable(
    client: TestClient, clock: ManualClock, payment: dict[str, Any], engine: Engine
) -> None:
    made_available(client, clock, payment)
    spend(client, payment["customer_account"], 5_000, AVAILABLE_AT)
    clock.set(OCTOBER)

    response = deliver_return(client, clock, payment, OCTOBER)

    assert response.json()["outcome"] == "applied"
    # The Customer Balance would be -50.00: the deficit moves to Recovery Receivable.
    assert customer_balances(client, payment) == (0, 0, 0)
    assert recovery_receivable(client, payment) == 5_000
    found = payments(client).payment(payment["id"])
    [exception] = found.return_exceptions
    assert exception.model_dump(exclude={"recovery_transaction_id", "inbox_id", "detail"}) == {
        "payment_id": payment["id"],
        "customer_account": payment["customer_account"],
        "recovery_account": f"recovery-receivable:{payment['customer_account']}",
        "currency": "USD",
        "return_code": "R10",
        "exposure": 5_000,
        "late": True,
        "effective_at": OCTOBER,
        "recorded_at": OCTOBER,
    }
    assert exception.inbox_id == response.json()["inbox_id"]
    assert "5000" in exception.detail and "R10" in exception.detail
    recovery = journal(client).transaction(exception.recovery_transaction_id)
    assert recovery.reverses is None and recovery.status == "posted"
    assert booked_on(engine, payment, CLEARING) == 0
    # Readable over the API, on the Payment and in the list of exceptions.
    on_payment = client.get(f"/payments/{payment['id']}").json()["return_exceptions"]
    assert [e["exposure"] for e in on_payment] == [5_000]
    listed = client.get(
        "/return-exceptions", params={"customer_account": payment["customer_account"]}
    ).json()
    assert [(e["payment_id"], e["exposure"]) for e in listed] == [(payment["id"], 5_000)]


def test_a_return_before_availability_of_money_already_spent_also_moves_the_deficit(
    client: TestClient, clock: ManualClock, payment: dict[str, Any]
) -> None:
    """Bought at an Auction Close before the funds were Available: the lot is booked anyway
    (ADR 0012), so the Return leaves the same deficit, but not as a Late Return."""
    settled(client, clock, payment)
    spend(client, payment["customer_account"], AMOUNT, SETTLED_AT)
    clock.set(MONDAY)

    deliver_return(client, clock, payment, MONDAY, code="R01")

    assert customer_balances(client, payment) == (0, 0, 0)
    assert recovery_receivable(client, payment) == AMOUNT
    [exception] = payments(client).payment(payment["id"]).return_exceptions
    assert (exception.exposure, exception.late, exception.return_code.code) == (
        AMOUNT,
        False,
        "R01",
    )


def test_a_return_is_applied_once(
    client: TestClient, clock: ManualClock, payment: dict[str, Any], engine: Engine
) -> None:
    made_available(client, clock, payment)
    spend(client, payment["customer_account"], 1_000, AVAILABLE_AT)
    clock.set(OCTOBER)
    first = deliver_return(client, clock, payment, OCTOBER, event_id=unique("evt-return"))
    before = (booked_on(engine, payment, BANK_CASH), recovery_receivable(client, payment))

    redelivered = deliver_return(client, clock, payment, OCTOBER, event_id=first.json()["event_id"])
    another = deliver_return(client, clock, payment, OCTOBER, code="R07")
    late_settlement = deliver(client, clock, payment, "payment.settled", SETTLED_AT)

    assert redelivered.json()["duplicate"] is True
    assert another.json()["outcome"] == late_settlement.json()["outcome"] == "stale"
    assert (booked_on(engine, payment, BANK_CASH), recovery_receivable(client, payment)) == before
    assert len(payments(client).payment(payment["id"]).return_exceptions) == 1


@pytest.mark.parametrize("before", ["created", "submitted"])
def test_a_return_for_a_payment_never_settled_is_an_exception_with_no_ledger_write(
    client: TestClient, clock: ManualClock, payment: dict[str, Any], engine: Engine, before: str
) -> None:
    if before == "submitted":
        deliver(client, clock, payment, "payment.submitted")
    clock.set(MONDAY)

    response = deliver_return(client, clock, payment, MONDAY)

    assert response.json()["outcome"] == "exception"
    assert "never settled" in response.json()["detail"]
    assert booked_on(engine, payment, CLEARING) == (AMOUNT if before == "submitted" else 0)
    assert payments(client).payment(payment["id"]).state == before


def test_a_return_must_carry_its_code(
    client: TestClient, clock: ManualClock, payment: dict[str, Any]
) -> None:
    body = event_body(payment, "payment.returned", SETTLED_AT, unique("evt"))

    response = client.post(
        "/providers/simulator/events",
        content=body,
        headers={SIGNATURE_HEADER: sign(SECRET, body, clock.now())},
    )

    assert response.status_code == 400


def test_a_payout_return_is_an_exception(client: TestClient, clock: ManualClock) -> None:
    payout = push(client, "ach", funded_customer(client, 10_000))
    deliver(client, clock, payout, "payment.submitted")
    clock.set(SETTLED_AT)
    deliver(client, clock, payout, "payment.settled", SETTLED_AT)

    response = deliver_return(client, clock, payout, MONDAY, code="R03")

    assert response.json()["outcome"] == "exception"
    assert "push" in response.json()["detail"]
    assert customer_balances(client, payout) == (10_000 - AMOUNT, 0, 10_000 - AMOUNT)


def another_pull(client: TestClient, payment: dict[str, Any]) -> dict[str, Any]:
    """A second ACH pull into the same Customer Balance."""
    response = client.post(
        "/payments",
        json={
            "idempotency_key": unique("pull"),
            "rail": "ach",
            "direction": "pull",
            "customer_account": payment["customer_account"],
            "amount": AMOUNT,
            "currency": "USD",
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def short_by(client: TestClient, customer: str, at: datetime) -> int:
    """How far the Customer Balance's Posted less unsettled outflows is below zero at `at`,
    as known now."""
    found = journal(client).balances(customer, at)
    return max(0, found.unsettled_outflows - found.posted)


def test_a_delayed_return_covers_the_deficit_at_every_time_from_its_effective_time(
    client: TestClient, clock: ManualClock, payment: dict[str, Any]
) -> None:
    """The Return takes effect on 5 October but the Ledger hears of it on the 20th. Meanwhile
    the Customer spent 20.00 on the 7th and a second pull settled on the 9th. From the
    Return's Effective Time on, as known now, the Customer Balance is never short: the
    deficit is moved where it occurs (75.00 on the 5th, 20.00 more on the 7th), and the
    later inflow doesn't quietly collect it."""
    customer = payment["customer_account"]
    made_available(client, clock, payment)
    spend(client, customer, AMOUNT, AVAILABLE_AT)
    spent_after = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
    inflow_settles = datetime(2026, 10, 9, 12, 30, tzinfo=UTC)
    second = another_pull(client, payment)
    clock.set(datetime(2026, 10, 8, 14, 0, tzinfo=UTC))
    deliver(client, clock, second, "payment.submitted", clock.now())
    clock.set(inflow_settles)
    deliver(client, clock, second, "payment.settled", inflow_settles)
    spend(client, customer, 2_000, spent_after)

    clock.set(datetime(2026, 10, 20, 14, 0, tzinfo=UTC))
    response = deliver_return(client, clock, payment, OCTOBER)

    assert response.json()["outcome"] == "applied", response.text
    for moment in (OCTOBER, spent_after, inflow_settles, clock.now()):
        assert short_by(client, customer, moment) == 0, moment
    assert payments(client).owed(customer) == AMOUNT + 2_000
    raised = payments(client).payment(payment["id"]).return_exceptions
    assert [(e.exposure, e.effective_at) for e in raised] == [
        (AMOUNT, OCTOBER),
        (2_000, spent_after),
    ]
    # What's left is the second pull's, not yet Available; the receivable stays owed.
    assert customer_balances(client, payment) == (AMOUNT, 0, 0)


@pytest.mark.parametrize("availability_job_first", [True, False])
@pytest.mark.parametrize(
    ("after_available", "late"), [(timedelta(0), False), (timedelta(seconds=1), True)]
)
def test_a_late_return_is_decided_by_time_not_by_when_the_availability_job_ran(
    client: TestClient,
    clock: ManualClock,
    payment: dict[str, Any],
    availability_job_first: bool,
    after_available: timedelta,
    late: bool,
) -> None:
    """The R01-R03 window and availability end at the same moment: a Return then is not
    late, one a second after is, whichever of the Return and the job the Ledger saw first."""
    settled(client, clock, payment)
    spend(client, payment["customer_account"], AMOUNT, SETTLED_AT)
    returned_at = AVAILABLE_AT + after_available
    clock.set(returned_at)
    if availability_job_first:
        payments(client).make_available()

    deliver_return(client, clock, payment, returned_at, code="R01")
    payments(client).make_available()

    [exception] = payments(client).payment(payment["id"]).return_exceptions
    assert exception.late is late


def test_two_returns_for_one_customer_at_once_move_the_whole_deficit_between_them(
    client: TestClient, clock: ManualClock, payment: dict[str, Any]
) -> None:
    """Funded 150.00 by two pulls, spent 112.50, both returned at once. Each alone would
    leave 37.50 short; together they leave 112.50 short. Returns on one Customer Balance are
    booked one at a time, so the second sees the first's Reversal and moves the rest, and the
    Recovery Receivable both open is opened once."""
    customer = payment["customer_account"]
    second = another_pull(client, payment)
    for pull in (payment, second):
        deliver(client, clock, pull, "payment.submitted")
    clock.set(SETTLED_AT)
    for pull in (payment, second):
        deliver(client, clock, pull, "payment.settled", SETTLED_AT)
    spend(client, customer, AMOUNT * 3 // 2, SETTLED_AT)
    clock.set(MONDAY)
    bodies = []
    for pull in (payment, second):
        body = json.dumps(
            {
                "event_id": unique("evt-return"),
                "type": "payment.returned",
                "occurred_at": MONDAY.isoformat(),
                "payment": {"id": pull["provider_payment_id"]},
                "return": {"code": "R01"},
            }
        ).encode()
        bodies.append((body, {SIGNATURE_HEADER: sign(SECRET, body, clock.now())}))
    start = Barrier(2)

    def apply(body: bytes, headers: dict[str, str]) -> str:
        start.wait()
        return payments(client).receive("simulator", body, headers).outcome

    with ThreadPoolExecutor(2) as pool:
        outcomes = [f.result() for f in [pool.submit(apply, *b) for b in bodies]]

    assert outcomes == ["applied", "applied"]
    assert payments(client).owed(customer) == AMOUNT * 3 // 2
    assert short_by(client, customer, MONDAY) == 0
    assert sorted(e.exposure for e in payments(client).return_exceptions(customer)) == [
        AMOUNT // 2,
        AMOUNT,
    ]
