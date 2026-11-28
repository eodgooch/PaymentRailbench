"""The ACH Payment state machine under random event orders (SPEC §4.2, §10, ADR 0011).

A `hypothesis.stateful` machine originates ACH pulls and plays a Provider that reports each
one submitted and then ended (settled, declined or canceled), sometimes contradicting itself
with a second, different ending. It delivers those Provider Events in any order, redelivers
them, lets time pass between deliveries, and runs the Ledger's availability job, all through
the real Payments service and Journal against Postgres, with signed webhook bodies.

After every step:

- every Transaction balances per currency;
- no Payment's state moves backward;
- each Provider Event is in the Inbox once and applied at most once;
- the Clearing Account nets to zero for a Payment that settled or ended, and holds exactly
  the amount for one still in flight;
- a stale or impossible event writes nothing to the Ledger, and the outcome of each
  delivery is the one the state machine's rules give (written out here as a table);
- each Customer Balance's Posted, Pending and Available match the Payment's furthest state.

At the end of each example, replaying its Inbox into an empty Ledger reproduces it exactly.
"""

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import Engine, create_engine, func, select

from railbench.common.clock import ManualClock
from railbench.common.db import LEDGER_APP_ROLE, make_engine
from railbench.common.signing import SIGNATURE_HEADER, sign
from railbench.ledger.journal import Journal
from railbench.ledger.model import AccountType, NewAccount
from railbench.ledger.tables import entries, hold_ends, holds, settlements
from railbench.payments.model import NewPayment
from railbench.payments.provider import SimulatorProvider
from railbench.payments.service import Payments, clearing_account
from railbench.scenarios.replay import replay_inbox
from railbench.scenarios.runner import fresh_database
from railbench.scenarios.summary import Summary, ledger_summary
from tests.integration.helpers import END_OF_TIME

pytestmark = pytest.mark.integration

SECRET = "property-secret"
START = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)  # a Tuesday morning in New York
CLEARING = clearing_account("simulator", "ach", "USD")
ENDINGS = ("settled", "declined", "canceled")
RANK = {"created": 0, "submitted": 1, "settled": 2, "available": 3, "declined": 4, "canceled": 4}
LEDGER_TABLES = (entries, settlements, holds, hold_ends)


def expected_outcome(furthest: str, reported: str) -> str:
    """What delivering `reported` to a Payment at `furthest` does, from SPEC §4.1-4.2: forward
    moves apply, a state already passed is stale, anything else is an exception."""
    if reported == "submitted":
        return "applied" if furthest == "created" else "stale"
    if furthest in ("declined", "canceled"):
        return "stale" if reported == furthest else "exception"
    if reported == "settled":
        return "applied" if furthest in ("created", "submitted") else "stale"
    # declined or canceled
    return "applied" if furthest in ("created", "submitted") else "exception"


def furthest_after(furthest: str, reported: str) -> str:
    if expected_outcome(furthest, reported) != "applied":
        return furthest
    return reported


@dataclass
class Event:
    event_id: str
    type: str
    occurred_at: datetime
    delivered: bool = False


@dataclass
class Pull:
    ledger_id: int
    provider_id: str
    customer: str
    amount: int
    events: list[Event]
    furthest: str = "created"
    seen_states: list[str] = field(default_factory=list)


class AchPullMachine(RuleBasedStateMachine):
    def __init__(self, engines: list[Engine], start: list[datetime], seen: Counter[str]) -> None:
        super().__init__()
        self.source, self.owner, self.target, self.replay_owner = engines
        # Each example starts after the last one ended, so the shared databases only ever
        # see time move forward (the availability job acts on every Payment in them).
        self.start = start
        self.seen = seen  # what the examples exercised, to prove the run wasn't vacuous
        self.clock = ManualClock(start[0])
        self.run = uuid4().hex[:10]
        provider = SimulatorProvider(
            "http://simulator.invalid",
            SECRET,
            http=httpx.Client(
                base_url="http://simulator.invalid",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        201,
                        json={
                            "id": f"simpay_{json.loads(request.content)['idempotency_key']}",
                            "status": "submitted",
                        },
                    )
                ),
            ),
        )
        self.journal = Journal(self.source, self.clock)
        self.payments = Payments(self.source, self.clock, self.journal, provider)
        self.pulls: list[Pull] = []
        self.ticks: list[datetime] = []

    def _pass_time(self, seconds: int = 1) -> None:
        self.clock.advance(timedelta(seconds=seconds))

    @precondition(lambda self: len(self.pulls) < 4)
    @rule(
        amount=st.integers(min_value=1, max_value=10**9),
        ending=st.sampled_from(ENDINGS),
        contradiction=st.none() | st.sampled_from(ENDINGS),
        settles_after=st.integers(min_value=60, max_value=3 * 24 * 3600),
    )
    def originate(
        self, amount: int, ending: str, contradiction: str | None, settles_after: int
    ) -> None:
        self._pass_time()
        n = len(self.pulls)
        customer = f"customer:prop-{self.run}-{n}:usd"
        self.journal.open_account(
            NewAccount(code=customer, type=AccountType.LIABILITY, currency="USD")
        )
        key = f"prop-{self.run}-{n}"
        created = self.payments.originate(
            NewPayment(
                idempotency_key=key,
                rail="ach",
                direction="pull",
                customer_account=customer,
                amount=amount,
                currency="USD",
            )
        ).payment
        now = self.clock.now()
        ended_at = now + timedelta(seconds=settles_after)
        story = [
            Event(f"evt-{key}-submitted", "submitted", now),
            Event(f"evt-{key}-{ending}", ending, ended_at),
        ]
        if contradiction is not None and contradiction != ending:
            story.append(
                Event(f"evt-{key}-{contradiction}", contradiction, ended_at + timedelta(hours=1))
            )
        self.pulls.append(Pull(created.id, created.provider_payment_id, customer, amount, story))

    @precondition(lambda self: self.pulls)
    @rule(data=st.data(), wait=st.integers(min_value=1, max_value=36 * 3600))
    def deliver(self, data: st.DataObject, wait: int) -> None:
        """Deliver any of the Provider's events, in any order, again if it was already sent,
        after some time (never before it happened)."""
        pull = data.draw(st.sampled_from(self.pulls))
        event = data.draw(st.sampled_from(pull.events))
        self.clock.set(max(self.clock.now(), event.occurred_at) + timedelta(seconds=wait))
        before = self._ledger_rows()
        expected = expected_outcome(pull.furthest, event.type) if not event.delivered else None

        body = json.dumps(
            {
                "event_id": event.event_id,
                "type": f"payment.{event.type}",
                "occurred_at": event.occurred_at.isoformat(),
                "payment": {"id": pull.provider_id},
            }
        ).encode()
        receipt = self.payments.receive(
            "simulator", body, {SIGNATURE_HEADER: sign(SECRET, body, self.clock.now())}
        )

        self.seen["duplicate" if receipt.duplicate else receipt.outcome] += 1
        assert receipt.duplicate == event.delivered
        if expected is not None:
            assert receipt.outcome == expected, (pull.furthest, event.type, receipt.detail)
            pull.furthest = furthest_after(pull.furthest, event.type)
        if receipt.duplicate or receipt.outcome != "applied":
            assert self._ledger_rows() == before  # nothing written
        event.delivered = True

    @precondition(lambda self: self.pulls)
    @rule(hours=st.integers(min_value=1, max_value=96))
    def run_the_availability_job(self, hours: int) -> None:
        self._pass_time(hours * 3600)
        self.payments.make_available()
        self.ticks.append(self.clock.now())
        for pull in self.pulls:
            payment = self.payments.payment(pull.ledger_id)
            due = payment.available_at
            if pull.furthest == "settled" and due is not None and due <= self.clock.now():
                pull.furthest = "available"
                self.seen["available"] += 1

    def _ledger_rows(self) -> tuple[int, ...]:
        with self.owner.connect() as connection:
            return tuple(
                connection.scalar(select(func.count()).select_from(t)) or 0 for t in LEDGER_TABLES
            )

    def _summary(self) -> Summary:
        return ledger_summary(self.owner, {p.customer for p in self.pulls})

    @invariant()
    def the_ledger_holds_together(self) -> None:
        summary = self._summary()
        transactions = summary["transactions"]
        for key, transaction in transactions.items():
            totals: dict[str, int] = {}
            for _account, currency, amount, *_ in transaction["entries"]:
                totals[currency] = totals.get(currency, 0) + amount
            assert set(totals.values()) <= {0}, key

        for pull in self.pulls:
            payment = self.payments.payment(pull.ledger_id)
            # Forward only, and the Ledger agrees with the rules about how far it got.
            assert payment.state == pull.furthest
            if pull.seen_states:
                previous = pull.seen_states[-1]
                assert RANK[payment.state] >= RANK[previous]
                if RANK[previous] == 4:
                    assert payment.state == previous
            pull.seen_states.append(payment.state)
            # Each state at most once; each Provider Event in the Inbox once, applied once.
            states = [t.state for t in payment.transitions]
            assert len(states) == len(set(states))
            found = summary["payments"][f"prop-{self.run}-{self.pulls.index(pull)}"]
            event_ids = [o["event_id"] for o in found["inbox_outcomes"]]
            assert len(event_ids) == len(set(event_ids))
            assert set(event_ids) == {e.event_id for e in pull.events if e.delivered}

            booked = {s["transaction"] for s in found["transitions"]} - {None}
            clearing = sum(
                entry[2]
                for key in booked
                for entry in transactions[key]["entries"]
                if entry[0] == CLEARING
            )
            assert clearing == (pull.amount if payment.state == "submitted" else 0)

            balances = self.journal.balances(pull.customer, END_OF_TIME)
            posted, pending, available = {
                "created": (0, 0, 0),
                "submitted": (0, pull.amount, 0),
                "settled": (pull.amount, 0, 0),
                "available": (pull.amount, 0, pull.amount),
                "declined": (0, 0, 0),
                "canceled": (0, 0, 0),
            }[payment.state]
            assert (balances.posted, balances.pending, balances.available) == (
                posted,
                pending,
                available,
            )

    def teardown(self) -> None:
        self.start[0] = self.clock.now() + timedelta(days=1)
        if not self.pulls:
            return
        accounts = {p.customer for p in self.pulls}
        replay_inbox(self.owner, self.target, self.ticks, accounts)
        assert ledger_summary(self.replay_owner, accounts) == self._summary()


@pytest.fixture
def databases(database_url: str) -> Iterator[tuple[str, str]]:
    """Two fresh databases: one the machine runs in, one each example is replayed into."""
    with (
        fresh_database(database_url, "props") as source,
        fresh_database(database_url, "replay") as target,
    ):
        yield source, target


def test_the_ach_payment_machine_keeps_its_invariants_under_any_event_order(
    databases: tuple[str, str],
) -> None:
    source_url, target_url = databases
    engines = [
        make_engine(source_url, role=LEDGER_APP_ROLE),
        create_engine(source_url),
        make_engine(target_url, role=LEDGER_APP_ROLE),
        create_engine(target_url),
    ]
    start = [START]
    seen: Counter[str] = Counter()

    class Machine(AchPullMachine):
        def __init__(self) -> None:
            super().__init__(engines, start, seen)

    try:
        run_state_machine_as_test(
            Machine,
            settings=settings(
                max_examples=30,
                stateful_step_count=20,
                deadline=None,
                derandomize=True,
                suppress_health_check=[HealthCheck.too_slow],
            ),
        )
    finally:
        for engine in engines:
            engine.dispose()
    assert set(seen) == {"applied", "stale", "exception", "duplicate", "available"}, seen
