"""An ACH pull end to end (SPEC §4.1-4.3, §6, ADR 0004, ADR 0005).

A buyer funds their Customer Balance with an ACH pull. The Ledger creates the Payment at the
Simulator through the Provider interface, receives signed Provider Events by webhook over
real HTTP, stores them in the Inbox, moves the Payment through its state machine and books
it: pending through the Clearing Account at submission, Posted at settlement, Available 2
Banking Days later. Both services run in-process on one ManualClock.

The pull settles on Friday 4 September 2026, before Labor Day (Monday 7 September), so
Available lands on Wednesday 9 September: the 2 Banking Days span a weekend and a Fed
holiday.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, select, text

from railbench.common.clock import ManualClock
from railbench.common.db import LEDGER_APP_ROLE, make_engine
from railbench.common.settings import Settings
from railbench.ledger.app import create_app as create_ledger_app
from railbench.ledger.journal import Journal
from railbench.ledger.model import AccountType, NewAccount
from railbench.payments.model import NewPayment
from railbench.payments.provider import SimulatorProvider
from railbench.payments.service import Payments
from railbench.payments.tables import inbox
from railbench.simulator.app import create_app as create_simulator_app
from railbench.simulator.provider import Simulator
from tests.integration.helpers import alembic_config, unique
from tests.integration.servers import free_port, serving

pytestmark = pytest.mark.integration

SECRET = "end-to-end-secret"
THURSDAY_3PM = datetime(2026, 9, 3, 19, 0, tzinfo=UTC)  # 15:00 New York, before Cutoff
FRIDAY_SETTLEMENT = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)  # 08:30 New York
TUESDAY_AFTER_LABOR_DAY = datetime(2026, 9, 8, 20, 0, tzinfo=UTC)
WEDNESDAY_START = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)  # midnight New York


@dataclass
class Stack:
    clock: ManualClock
    ledger: httpx.Client
    simulator: Simulator
    payments: Payments
    journal: Journal

    def tick(self) -> None:
        """What both running services do on their timers: the Simulator settles and
        delivers webhooks, then the Ledger makes due funds Available."""
        self.simulator.tick()
        self.payments.make_available()

    def balances(self, code: str) -> dict[str, int]:
        found = self.journal.balances(code, self.clock.now())
        return {"posted": found.posted, "pending": found.pending, "available": found.available}


@pytest.fixture
def stack(engine: Engine, app_engine: Engine) -> Iterator[Stack]:
    clock = ManualClock(THURSDAY_3PM)
    ledger_port, simulator_port = free_port(), free_port()
    ledger_url = f"http://127.0.0.1:{ledger_port}"
    settings = Settings(
        simulator_url=f"http://127.0.0.1:{simulator_port}",
        ledger_webhook_url=f"{ledger_url}/providers/simulator/events",
        webhook_signing_secret=SECRET,
    )
    ledger_app = create_ledger_app(app_engine, clock, settings)
    simulator_app = create_simulator_app(engine, clock, settings)
    with (
        serving(simulator_app, simulator_port),
        serving(ledger_app, ledger_port),
        httpx.Client(base_url=ledger_url) as ledger,
    ):
        yield Stack(
            clock=clock,
            ledger=ledger,
            simulator=simulator_app.state.simulator,
            payments=ledger_app.state.payments,
            journal=ledger_app.state.journal,
        )


def buyer(stack: Stack) -> str:
    code = unique("customer")
    stack.journal.open_account(NewAccount(code=code, type=AccountType.LIABILITY, currency="USD"))
    return code


def pull(stack: Stack, customer: str, amount: int = 25_000) -> dict[str, Any]:
    response = stack.ledger.post(
        "/payments",
        json={
            "idempotency_key": unique("buyer-funding"),
            "rail": "ach",
            "direction": "pull",
            "customer_account": customer,
            "amount": amount,
            "currency": "USD",
            "description": "Buyer funds their Customer Balance",
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def state(stack: Stack, payment_id: int) -> str:
    found: str = stack.ledger.get(f"/payments/{payment_id}").json()["state"]
    return found


CLEARING = "clearing:simulator:ach:usd"
BANK_CASH = "bank-cash:usd"


def clearing_and_bank_cash(stack: Stack, payment_id: int) -> dict[str, dict[str, int]]:
    """What this Payment's own Entries add up to on the Clearing Account and Bank Cash,
    settled and not. Other tests share both Accounts, so their balances can't be compared."""
    totals = {code: {"settled": 0, "unsettled": 0} for code in (CLEARING, BANK_CASH)}
    for step in stack.ledger.get(f"/payments/{payment_id}").json()["transitions"]:
        if step["transaction_id"] is None:
            continue
        transaction = stack.journal.transaction(step["transaction_id"])
        settled = transaction.status == "posted" or transaction.settlement is not None
        for entry in transaction.entries:
            if entry.account in totals:
                totals[entry.account]["settled" if settled else "unsettled"] += entry.amount
    return totals


def test_an_ach_pull_is_pending_then_posted_then_available_two_banking_days_later(
    stack: Stack,
) -> None:
    customer = buyer(stack)

    payment = pull(stack, customer, 25_000)
    assert payment["state"] == "created"
    assert payment["provider"] == "simulator"
    assert payment["provider_payment_id"].startswith("simpay_")
    assert stack.balances(customer) == {"posted": 0, "pending": 0, "available": 0}

    # Thursday: the Simulator reports the pull submitted. Pending, not Available.
    stack.tick()
    assert state(stack, payment["id"]) == "submitted"
    assert stack.balances(customer) == {"posted": 0, "pending": 25_000, "available": 0}

    # Friday 08:30 New York: settled. Posted, but held until Available.
    stack.clock.set(FRIDAY_SETTLEMENT)
    stack.tick()
    assert state(stack, payment["id"]) == "settled"
    assert stack.balances(customer) == {"posted": 25_000, "pending": 0, "available": 0}
    # The Clearing Account has passed the money on to Bank Cash: it nets to zero again.
    assert clearing_and_bank_cash(stack, payment["id"]) == {
        CLEARING: {"settled": 0, "unsettled": 0},
        BANK_CASH: {"settled": 25_000, "unsettled": 0},
    }

    # The weekend, Labor Day and Tuesday: still not Available.
    for moment in (
        datetime(2026, 9, 5, 16, 0, tzinfo=UTC),
        datetime(2026, 9, 7, 16, 0, tzinfo=UTC),
        TUESDAY_AFTER_LABOR_DAY,
        WEDNESDAY_START - timedelta(seconds=1),
    ):
        stack.clock.set(moment)
        stack.tick()
        assert state(stack, payment["id"]) == "settled"
        assert stack.balances(customer)["available"] == 0

    # Wednesday, the second Banking Day after settlement: Available.
    stack.clock.set(WEDNESDAY_START)
    stack.tick()
    found = stack.ledger.get(f"/payments/{payment['id']}").json()
    assert found["state"] == "available"
    assert found["available_at"] == "2026-09-09T04:00:00Z"
    assert stack.balances(customer) == {"posted": 25_000, "pending": 0, "available": 25_000}
    assert [t["state"] for t in found["transitions"]] == [
        "created",
        "submitted",
        "settled",
        "available",
    ]


def test_the_ledger_rows_name_the_provider_events_behind_them(stack: Stack) -> None:
    customer = buyer(stack)
    payment = pull(stack, customer, 4_200)
    stack.tick()
    stack.clock.set(FRIDAY_SETTLEMENT)
    stack.tick()

    found = stack.ledger.get(f"/payments/{payment['id']}").json()
    submitted, settled = found["transitions"][1], found["transitions"][2]
    pending = stack.journal.transaction(submitted["transaction_id"])
    assert pending.status == "pending"
    assert {e.actor.model_dump()["inbox_id"] for e in pending.entries} == {
        str(submitted["inbox_id"])
    }
    assert pending.settlement is not None
    assert pending.settlement.effective_at == FRIDAY_SETTLEMENT
    assert pending.settlement.actor.model_dump()["inbox_id"] == str(settled["inbox_id"])
    hold = stack.journal.hold(settled["hold_id"])
    assert (hold.account, hold.amount, hold.ended) == (customer, 4_200, None)


def ledger_trace(engine: Engine, payments: Payments, journal: Journal, payment_id: int) -> Any:
    """Everything the Ledger booked for one Payment, with each Actor's Inbox ID swapped for
    its Provider event ID, so two databases' bookings compare equal when they match."""
    with engine.connect() as connection:
        event_ids: dict[str, str] = {
            str(row.id): row.event_id
            for row in connection.execute(select(inbox.c.id, inbox.c.event_id))
        }

    def actor(found: Any) -> str:
        data = found.model_dump()
        if data["kind"] == "provider_event":
            return event_ids[data["inbox_id"]]
        name: str = data["name"]
        return name

    trace = []
    for step in payments.payment(payment_id).transitions:
        booked: dict[str, Any] = {"state": step.state, "effective_at": step.effective_at}
        if step.transaction_id is not None:
            transaction = journal.transaction(step.transaction_id)
            booked["entries"] = [
                (entry.account, entry.amount, entry.effective_at, actor(entry.actor))
                for entry in transaction.entries
            ]
            booked["status"] = transaction.status
            settlement = journal.transaction(transaction.reverses or transaction.id).settlement
            booked["settled"] = settlement and (settlement.effective_at, actor(settlement.actor))
        if step.hold_id is not None:
            hold = journal.hold(step.hold_id)
            booked["hold"] = (hold.account, hold.amount, hold.effective_at, actor(hold.actor))
            booked["hold_end"] = hold.ended and (hold.ended.outcome, hold.ended.effective_at)
        trace.append(booked)
    return trace


@pytest.fixture
def empty_ledger(database_url: str) -> Iterator[str]:
    """A second database in the same Postgres, migrated and empty."""
    name = f"replay_{uuid4().hex[:8]}"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(database_url).set(database=name).render_as_string(hide_password=False)
    command.upgrade(alembic_config(url), "head")
    yield url
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
    admin.dispose()


def test_replaying_the_inbox_reproduces_the_ledger(
    stack: Stack, engine: Engine, empty_ledger: str
) -> None:
    customer = buyer(stack)
    payment = pull(stack, customer, 31_000)
    for moment in (THURSDAY_3PM, FRIDAY_SETTLEMENT, WEDNESDAY_START):
        stack.clock.set(moment)
        stack.tick()
    original = ledger_trace(engine, stack.payments, stack.journal, payment["id"])
    with engine.connect() as connection:
        events = connection.execute(
            select(inbox.c.body, inbox.c.headers)
            .where(
                inbox.c.event_id.in_(
                    [d.event_id for d in stack.simulator.deliveries(payment["provider_payment_id"])]
                )
            )
            .order_by(inbox.c.id)
        ).all()

    # The same Payment, recorded in an empty Ledger, then its Inbox replayed in order.
    replay_engine = make_engine(empty_ledger, role=LEDGER_APP_ROLE)
    owner_engine = create_engine(empty_ledger)
    try:
        clock = ManualClock(THURSDAY_3PM)
        provider_id = payment["provider_payment_id"]
        provider = SimulatorProvider(
            "http://simulator.invalid",
            SECRET,
            http=httpx.Client(
                base_url="http://simulator.invalid",
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        201, json={"id": provider_id, "status": "submitted"}
                    )
                ),
            ),
        )
        journal = Journal(replay_engine, clock)
        payments = Payments(replay_engine, clock, journal, {"simulator": provider})
        journal.open_account(NewAccount(code=customer, type=AccountType.LIABILITY, currency="USD"))
        replayed = payments.originate(
            NewPayment.model_validate(
                {key: payment[key] for key in NewPayment.model_fields if key in payment}
            )
        ).payment
        clock.set(WEDNESDAY_START)
        for body, headers in events:
            assert payments.replay("simulator", body, headers).outcome == "applied"
        payments.make_available()

        assert ledger_trace(owner_engine, payments, journal, replayed.id) == original
        assert journal.balances(customer, WEDNESDAY_START) == stack.journal.balances(
            customer, WEDNESDAY_START
        ).model_copy(update={"known_at": WEDNESDAY_START})
    finally:
        replay_engine.dispose()
        owner_engine.dispose()
