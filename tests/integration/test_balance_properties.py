"""Properties of bitemporal balances under generated histories, against a real Postgres
(SPEC §3.3, ADR 0002, ADR 0009).

A random history runs against a Customer Balance (a liability) and a Clearing Account (an
asset): posted and pending Transactions, Settlements, declines and Returns, and Holds on
either Account that are released or captured (by a posted or a pending Transaction), with
backdated Effective Times. The oracle is a plain replay of that history, written from the
GLOSSARY definitions rather than from the Journal's query: at (T, T') each Transaction is
unknown, posted, settled, declined or in flight, each Hold is open or not, and

    Posted    = the Entries of posted and settled Transactions
    Pending   = the Entries of in-flight Transactions
    outflows  = what each in-flight Transaction takes out of the Account
    Available = Posted - open Holds - outflows

Two properties:

- at any Effective Time and Recorded Time, the Journal's Balances match the oracle
- Balances as known at T' depend only on what was recorded by T': asking again after more
  history has been recorded gives the same answer

There's no balance cache to check against (ADR 0009): every read is the derivation.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, auto

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine

from railbench.common.clock import ManualClock
from railbench.ledger.journal import Journal
from railbench.ledger.model import (
    AccountType,
    Balances,
    NewAccount,
    NewEntry,
    NewHold,
    NewTransaction,
    ReleaseRequest,
    ReversalRequest,
    SettlementRequest,
    SystemJob,
    TransactionStatus,
)
from tests.integration.helpers import unique

pytestmark = pytest.mark.integration

START = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)
ACTOR = SystemJob(name="property-test", version="1")
HOURS = 96  # Effective Times fall within four days of START, so they often tie and cross

database = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

Moment = tuple[datetime, datetime]  # (Effective Time, Recorded Time)


def seen(moment: Moment | None, effective_at: datetime, known_at: datetime) -> bool:
    return moment is not None and moment[0] <= effective_at and moment[1] <= known_at


class State(Enum):
    UNKNOWN = auto()
    POSTED = auto()
    SETTLED = auto()
    DECLINED = auto()
    IN_FLIGHT = auto()


@dataclass
class Txn:
    id: int
    raw: dict[str, int]  # debit-positive amount per Account code
    pending: bool
    posted: Moment
    is_return: bool = False
    settled: Moment | None = None
    declined: Moment | None = None

    def state(self, effective_at: datetime, known_at: datetime) -> State:
        if not seen(self.posted, effective_at, known_at):
            return State.UNKNOWN
        if not self.pending:
            return State.POSTED
        if seen(self.settled, effective_at, known_at):
            return State.SETTLED
        if seen(self.declined, effective_at, known_at):
            return State.DECLINED
        return State.IN_FLIGHT


@dataclass
class HoldRow:
    id: int
    account: str
    amount: int
    placed: Moment
    ended: Moment | None = None


class History:
    """Drives the Journal and records the same history for the oracle."""

    def __init__(self, journal: Journal, clock: ManualClock) -> None:
        self.journal, self.clock = journal, clock
        self.customer, self.clearing = unique("customer"), unique("clearing")
        self.sign = {self.customer: -1, self.clearing: 1}  # normal direction
        journal.open_account(
            NewAccount(code=self.customer, type=AccountType.LIABILITY, currency="USD")
        )
        journal.open_account(NewAccount(code=self.clearing, type=AccountType.ASSET, currency="USD"))
        self.txns: list[Txn] = []
        self.holds: list[HoldRow] = []
        self.reversed: set[int] = set()

    # The oracle

    def expected(self, account: str, effective_at: datetime, known_at: datetime) -> Balances:
        posted = pending = outflows = 0
        for txn in self.txns:
            amount = txn.raw.get(account, 0) * self.sign[account]
            match txn.state(effective_at, known_at):
                case State.POSTED | State.SETTLED:
                    posted += amount
                case State.IN_FLIGHT:
                    pending += amount
                    outflows += max(0, -amount)
                case State.UNKNOWN | State.DECLINED:
                    pass
        held = sum(
            hold.amount
            for hold in self.holds
            if hold.account == account
            and seen(hold.placed, effective_at, known_at)
            and not seen(hold.ended, effective_at, known_at)
        )
        return Balances(
            account=account,
            currency="USD",
            effective_at=effective_at,
            known_at=known_at,
            posted=posted,
            pending=pending,
            held=held,
            unsettled_outflows=outflows,
            available=posted - held - outflows,
        )

    # Operations

    def _entries(self, customer_raw: int) -> list[NewEntry]:
        return [
            NewEntry(account=self.customer, currency="USD", amount=customer_raw),
            NewEntry(account=self.clearing, currency="USD", amount=-customer_raw),
        ]

    def _record(self, txn_id: int, customer_raw: int, pending: bool, posted: Moment) -> None:
        raw = {self.customer: customer_raw, self.clearing: -customer_raw}
        self.txns.append(Txn(txn_id, raw, pending, posted))

    def post(self, customer_raw: int, pending: bool, effective_at: datetime) -> None:
        posted = self.journal.post(
            NewTransaction(
                idempotency_key=unique("txn"),
                actor=ACTOR,
                effective_at=effective_at,
                status=TransactionStatus.PENDING if pending else TransactionStatus.POSTED,
                entries=self._entries(customer_raw),
            )
        ).transaction
        self._record(posted.id, customer_raw, pending, (effective_at, posted.recorded_at))

    def settle(self, txn: Txn, after: timedelta) -> None:
        effective_at = txn.posted[0] + after
        settled = self.journal.settle(
            txn.id,
            SettlementRequest(
                idempotency_key=unique("settle"), actor=ACTOR, effective_at=effective_at
            ),
        ).transaction
        assert settled.settlement is not None
        txn.settled = (effective_at, settled.settlement.recorded_at)

    def reverse(self, txn: Txn, after: timedelta) -> None:
        earliest = txn.settled[0] if txn.settled else txn.posted[0]
        effective_at = max(earliest, txn.posted[0]) + after
        reversal = self.journal.reverse(
            txn.id,
            ReversalRequest(
                idempotency_key=unique("reverse"), actor=ACTOR, effective_at=effective_at
            ),
        ).transaction
        self.reversed.add(txn.id)
        if txn.pending and txn.settled is None:
            assert reversal.status is TransactionStatus.PENDING
            txn.declined = (effective_at, reversal.recorded_at)
        else:
            assert reversal.status is TransactionStatus.POSTED
            returned = Txn(
                reversal.id,
                {code: -amount for code, amount in txn.raw.items()},
                pending=False,
                posted=(effective_at, reversal.recorded_at),
                is_return=True,
            )
            self.txns.append(returned)

    def place(self, account: str, amount: int, effective_at: datetime) -> None:
        hold = self.journal.place_hold(
            NewHold(
                idempotency_key=unique("hold"),
                actor=ACTOR,
                effective_at=effective_at,
                account=account,
                currency="USD",
                amount=amount,
            )
        ).hold
        self.holds.append(HoldRow(hold.id, account, amount, (effective_at, hold.recorded_at)))

    def release(self, hold: HoldRow, after: timedelta) -> None:
        effective_at = hold.placed[0] + after
        ended = self.journal.release_hold(
            hold.id,
            ReleaseRequest(
                idempotency_key=unique("release"), actor=ACTOR, effective_at=effective_at
            ),
        ).hold.ended
        assert ended is not None
        hold.ended = (effective_at, ended.recorded_at)

    def capture(self, hold: HoldRow, amount: int, pending: bool, after: timedelta) -> None:
        effective_at = hold.placed[0] + after
        # Debit the Customer Balance and credit Clearing: money out of both, in their
        # normal directions, so it captures a Hold on either.
        captured = self.journal.capture_hold(
            hold.id,
            NewTransaction(
                idempotency_key=unique("capture"),
                actor=ACTOR,
                effective_at=effective_at,
                status=TransactionStatus.PENDING if pending else TransactionStatus.POSTED,
                entries=self._entries(amount),
            ),
        ).transaction
        hold.ended = (effective_at, captured.recorded_at)
        self._record(captured.id, amount, pending, (effective_at, captured.recorded_at))


hours = st.integers(min_value=0, max_value=HOURS).map(lambda h: START + timedelta(hours=h))
delays = st.integers(min_value=0, max_value=48).map(lambda h: timedelta(hours=h))
amounts = st.integers(min_value=-50_000, max_value=50_000).filter(bool)
positive = st.integers(min_value=1, max_value=50_000)


def run_history(data: st.DataObject, history: History, steps: int) -> list[Balances]:
    """Apply `steps` random operations, one simulated hour apart. After each, read both
    Accounts' Balances at a random Effective Time as known now, and return the readings."""
    readings: list[Balances] = []
    for _ in range(steps):
        history.clock.advance(timedelta(hours=1))
        originals = [t for t in history.txns if not t.is_return and t.id not in history.reversed]
        settleable = [t for t in originals if t.pending and t.settled is None]
        open_holds = [h for h in history.holds if h.ended is None]
        possible = {
            "post": True,
            "settle": bool(settleable),
            "reverse": bool(originals),
            "place": True,
            "release": bool(open_holds),
            "capture": bool(open_holds),
        }
        match data.draw(st.sampled_from([op for op, ok in possible.items() if ok])):
            case "post":
                history.post(data.draw(amounts), data.draw(st.booleans()), data.draw(hours))
            case "settle":
                history.settle(data.draw(st.sampled_from(settleable)), data.draw(delays))
            case "reverse":
                history.reverse(data.draw(st.sampled_from(originals)), data.draw(delays))
            case "place":
                account = data.draw(st.sampled_from([history.customer, history.clearing]))
                history.place(account, data.draw(positive), data.draw(hours))
            case "release":
                history.release(data.draw(st.sampled_from(open_holds)), data.draw(delays))
            case "capture":
                history.capture(
                    data.draw(st.sampled_from(open_holds)),
                    data.draw(positive),
                    data.draw(st.booleans()),
                    data.draw(delays),
                )
        effective_at = data.draw(hours)
        for account in (history.customer, history.clearing):
            readings.append(history.journal.balances(account, effective_at))
    return readings


def new_history(app_engine: Engine) -> History:
    clock = ManualClock(START)
    return History(Journal(app_engine, clock), clock)


@database
@given(data=st.data(), steps=st.integers(min_value=1, max_value=12))
def test_balances_match_the_definitions_at_every_time(
    app_engine: Engine, data: st.DataObject, steps: int
) -> None:
    history = new_history(app_engine)
    run_history(data, history, steps)
    known = sorted(
        {t.posted[1] for t in history.txns}
        | {t.settled[1] for t in history.txns if t.settled}
        | {t.declined[1] for t in history.txns if t.declined}
        | {h.placed[1] for h in history.holds}
        | {h.ended[1] for h in history.holds if h.ended}
        | {history.clock.now()}
    )

    for _ in range(8):
        effective_at = data.draw(hours | st.just(START + timedelta(days=30)))
        known_at = data.draw(st.sampled_from(known))
        for account in (history.customer, history.clearing):
            assert history.journal.balances(account, effective_at, known_at) == (
                history.expected(account, effective_at, known_at)
            )


@database
@given(data=st.data(), steps=st.integers(min_value=1, max_value=12))
def test_balances_as_known_then_never_change(
    app_engine: Engine, data: st.DataObject, steps: int
) -> None:
    history = new_history(app_engine)

    readings = run_history(data, history, steps)

    for reading in readings:
        assert history.journal.balances(
            reading.account, reading.effective_at, reading.known_at
        ) == (reading)
