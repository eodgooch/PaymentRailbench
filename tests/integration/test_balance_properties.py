"""Properties of bitemporal balances under generated histories, against a real Postgres
(SPEC §3.3, ADR 0002, ADR 0009).

A random history of posted and pending Transactions, Settlements, Reversals and Holds
(placed, released, captured), with backdated Effective Times, runs against one Customer
Balance. A plain-Python model of the same history, written from the GLOSSARY definitions,
is the oracle:

- at any Effective Time and Recorded Time, the Journal's Posted, Pending, held and
  unsettled-outflow totals match the model, and Available = Posted - Holds - unsettled
  outflows
- a balance as known at T' depends only on what was recorded by T': asking again after
  more history has been recorded gives the same answer

There's no balance cache to check against (ADR 0009): every read is the derivation.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine

from railbench.common.clock import ManualClock
from railbench.ledger.journal import Journal
from railbench.ledger.model import (
    AccountType,
    Balance,
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


@dataclass
class Txn:
    """One Transaction's effect on the Customer Balance, in its normal direction."""

    id: int
    delta: int
    pending: bool
    effective_at: datetime
    recorded_at: datetime
    base: int  # itself, or for a Reversal the Transaction it reverses


@dataclass
class HoldRow:
    id: int
    amount: int
    effective_at: datetime
    recorded_at: datetime
    ended: tuple[datetime, datetime] | None = None  # (effective, recorded)


@dataclass
class Model:
    txns: list[Txn] = field(default_factory=list)
    settlements: dict[int, tuple[datetime, datetime]] = field(default_factory=dict)
    holds: list[HoldRow] = field(default_factory=list)
    reversed: set[int] = field(default_factory=set)

    def balance(self, effective_at: datetime, known_at: datetime) -> tuple[int, int, int, int]:
        """(posted, pending, held, unsettled outflows), from the GLOSSARY definitions."""

        def counts(effective: datetime, recorded: datetime) -> bool:
            return effective <= effective_at and recorded <= known_at

        posted = 0
        unsettled: dict[int, int] = {}
        for txn in self.txns:
            if not counts(txn.effective_at, txn.recorded_at):
                continue
            settlement = self.settlements.get(txn.base)
            if not txn.pending or (settlement is not None and counts(*settlement)):
                posted += txn.delta
            else:
                unsettled[txn.base] = unsettled.get(txn.base, 0) + txn.delta
        held = sum(
            hold.amount
            for hold in self.holds
            if counts(hold.effective_at, hold.recorded_at)
            and not (hold.ended is not None and counts(*hold.ended))
        )
        outflows = sum(-net for net in unsettled.values() if net < 0)
        return posted, sum(unsettled.values()), held, outflows


class History:
    """Drives the Journal and the Model with the same operations."""

    def __init__(self, journal: Journal, clock: ManualClock) -> None:
        self.journal, self.clock, self.model = journal, clock, Model()
        self.customer, self.clearing = unique("customer"), unique("clearing")
        journal.open_account(
            NewAccount(code=self.customer, type=AccountType.LIABILITY, currency="USD")
        )
        journal.open_account(NewAccount(code=self.clearing, type=AccountType.ASSET, currency="USD"))

    def _entries(self, delta: int) -> list[NewEntry]:
        # The Customer Balance is a liability: +delta in its normal direction is a credit.
        return [
            NewEntry(account=self.customer, currency="USD", amount=-delta),
            NewEntry(account=self.clearing, currency="USD", amount=delta),
        ]

    def post(self, delta: int, pending: bool, effective_at: datetime) -> None:
        posted = self.journal.post(
            NewTransaction(
                idempotency_key=unique("txn"),
                actor=ACTOR,
                effective_at=effective_at,
                status=TransactionStatus.PENDING if pending else TransactionStatus.POSTED,
                entries=self._entries(delta),
            )
        ).transaction
        self.model.txns.append(
            Txn(posted.id, delta, pending, effective_at, posted.recorded_at, posted.id)
        )

    def settle(self, txn: Txn, after: timedelta) -> None:
        effective_at = txn.effective_at + after
        settled = self.journal.settle(
            txn.id,
            SettlementRequest(
                idempotency_key=unique("settle"), actor=ACTOR, effective_at=effective_at
            ),
        ).transaction
        assert settled.settlement is not None
        self.model.settlements[txn.id] = (effective_at, settled.settlement.recorded_at)

    def reverse(self, txn: Txn, effective_at: datetime) -> None:
        reversal = self.journal.reverse(
            txn.id,
            ReversalRequest(
                idempotency_key=unique("reverse"), actor=ACTOR, effective_at=effective_at
            ),
        ).transaction
        self.model.reversed.add(txn.id)
        self.model.txns.append(
            Txn(reversal.id, -txn.delta, txn.pending, effective_at, reversal.recorded_at, txn.id)
        )

    def place(self, amount: int, effective_at: datetime) -> None:
        hold = self.journal.place_hold(
            NewHold(
                idempotency_key=unique("hold"),
                actor=ACTOR,
                effective_at=effective_at,
                account=self.customer,
                currency="USD",
                amount=amount,
            )
        ).hold
        self.model.holds.append(HoldRow(hold.id, amount, effective_at, hold.recorded_at))

    def release(self, hold: HoldRow, after: timedelta) -> None:
        effective_at = hold.effective_at + after
        ended = self.journal.release_hold(
            hold.id,
            ReleaseRequest(
                idempotency_key=unique("release"), actor=ACTOR, effective_at=effective_at
            ),
        ).hold.ended
        assert ended is not None
        hold.ended = (effective_at, ended.recorded_at)

    def capture(self, hold: HoldRow, amount: int, after: timedelta) -> None:
        effective_at = hold.effective_at + after
        captured = self.journal.capture_hold(
            hold.id,
            NewTransaction(
                idempotency_key=unique("capture"),
                actor=ACTOR,
                effective_at=effective_at,
                entries=self._entries(-amount),
            ),
        ).transaction
        hold.ended = (effective_at, captured.recorded_at)
        self.model.txns.append(
            Txn(captured.id, -amount, False, effective_at, captured.recorded_at, captured.id)
        )

    def balance(self, effective_at: datetime, known_at: datetime | None = None) -> Balance:
        return self.journal.balance(self.customer, effective_at, known_at)


hours = st.integers(min_value=0, max_value=HOURS).map(lambda h: START + timedelta(hours=h))
delays = st.integers(min_value=0, max_value=48).map(lambda h: timedelta(hours=h))
amounts = st.integers(min_value=-50_000, max_value=50_000).filter(bool)
hold_amounts = st.integers(min_value=1, max_value=50_000)
OPERATIONS = ("post", "settle", "reverse", "place", "release", "capture")


def run_history(data: st.DataObject, history: History, steps: int) -> list[Balance]:
    """Apply `steps` random operations, one simulated hour apart. After each, read the
    balance at a random Effective Time as known now, and return those readings."""
    readings: list[Balance] = []
    for _ in range(steps):
        history.clock.advance(timedelta(hours=1))
        model = history.model
        settleable = [
            t
            for t in model.txns
            if t.pending
            and t.base == t.id
            and t.id not in model.settlements
            and t.id not in model.reversed
        ]
        reversible = [t for t in model.txns if t.base == t.id and t.id not in model.reversed]
        open_holds = [h for h in model.holds if h.ended is None]
        choices = [
            op
            for op, possible in zip(
                OPERATIONS,
                (
                    True,
                    bool(settleable),
                    bool(reversible),
                    True,
                    bool(open_holds),
                    bool(open_holds),
                ),
                strict=True,
            )
            if possible
        ]
        match data.draw(st.sampled_from(choices)):
            case "post":
                history.post(data.draw(amounts), data.draw(st.booleans()), data.draw(hours))
            case "settle":
                history.settle(data.draw(st.sampled_from(settleable)), data.draw(delays))
            case "reverse":
                history.reverse(data.draw(st.sampled_from(reversible)), data.draw(hours))
            case "place":
                history.place(data.draw(hold_amounts), data.draw(hours))
            case "release":
                history.release(data.draw(st.sampled_from(open_holds)), data.draw(delays))
            case "capture":
                history.capture(
                    data.draw(st.sampled_from(open_holds)),
                    data.draw(hold_amounts),
                    data.draw(delays),
                )
        readings.append(history.balance(data.draw(hours)))
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
    model = history.model
    known = sorted(
        {t.recorded_at for t in model.txns}
        | {h.recorded_at for h in model.holds}
        | {recorded for _, recorded in model.settlements.values()}
        | {history.clock.now()}
    )

    for _ in range(8):
        effective_at = data.draw(hours | st.sampled_from([START + timedelta(days=30)]))
        known_at = data.draw(st.sampled_from(known))
        balance = history.balance(effective_at, known_at)

        expected = model.balance(effective_at, known_at)
        assert (balance.posted, balance.pending, balance.held, balance.unsettled_outflows) == (
            expected
        )
        posted, _, held, outflows = expected
        assert balance.available == posted - held - outflows


@database
@given(data=st.data(), steps=st.integers(min_value=1, max_value=12))
def test_a_balance_as_known_then_never_changes(
    app_engine: Engine, data: st.DataObject, steps: int
) -> None:
    history = new_history(app_engine)

    readings = run_history(data, history, steps)

    for reading in readings:
        assert history.balance(reading.effective_at, reading.known_at) == reading
