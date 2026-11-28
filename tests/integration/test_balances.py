"""Bitemporal balances (SPEC §3.3, ADR 0002, ADR 0009): Posted, Pending and Available, at an
Effective Time, as known at a Recorded Time."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine

from railbench.common.clock import ManualClock
from railbench.ledger.journal import (
    AccountNotFound,
    AlreadyReversed,
    AlreadySettled,
    IdempotencyConflict,
    Journal,
    NotSettleable,
    NotYetKnown,
    TooEarly,
    TransactionNotFound,
)
from railbench.ledger.model import (
    AccountType,
    NewAccount,
    NewEntry,
    NewTransaction,
    ReversalRequest,
    SettlementRequest,
    SystemJob,
    TransactionStatus,
)
from tests.integration.helpers import unique

pytestmark = pytest.mark.integration

MONDAY = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)
TUESDAY = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)
ACH_JOB = SystemJob(name="ach-settlement", version="1.0.0")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(MONDAY)


@pytest.fixture
def journal(app_engine: Engine, clock: ManualClock) -> Journal:
    return Journal(app_engine, clock)


def open_accounts(journal: Journal) -> tuple[str, str]:
    """A Clearing Account (asset) and a Customer Balance (liability) in USD."""
    clearing, customer = unique("clearing"), unique("customer")
    journal.open_account(NewAccount(code=clearing, type=AccountType.ASSET, currency="USD"))
    journal.open_account(NewAccount(code=customer, type=AccountType.LIABILITY, currency="USD"))
    return clearing, customer


def funding(
    clearing: str,
    customer: str,
    amount: int,
    effective_at: datetime = MONDAY,
    status: TransactionStatus = TransactionStatus.POSTED,
) -> NewTransaction:
    """Money in for the Customer: debit Clearing, credit the Customer Balance."""
    return NewTransaction(
        idempotency_key=unique("funding"),
        actor=ACH_JOB,
        effective_at=effective_at,
        status=status,
        entries=[
            NewEntry(account=clearing, currency="USD", amount=amount),
            NewEntry(account=customer, currency="USD", amount=-amount),
        ],
    )


def test_a_customer_balance_reads_in_its_normal_direction(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)

    journal.post(funding(clearing, customer, 3_000))

    # A credit to a liability is money Mintwharf owes the Customer: it reads as +3,000.
    assert journal.balances(customer, MONDAY).posted == 3_000
    assert journal.balances(clearing, MONDAY).posted == 3_000


def test_a_backdated_entry_changes_as_known_now_but_not_as_known_then(
    journal: Journal, clock: ManualClock
) -> None:
    clearing, customer = open_accounts(journal)
    journal.post(funding(clearing, customer, 3_000))
    clock.set(WEDNESDAY)

    # Learned on Wednesday: a Provider Adjustment that took effect on Tuesday.
    journal.post(funding(clearing, customer, -500, effective_at=TUESDAY))

    assert journal.balances(customer, TUESDAY, known_at=TUESDAY).posted == 3_000
    assert journal.balances(customer, TUESDAY).posted == 2_500
    assert journal.balances(customer, TUESDAY, known_at=WEDNESDAY).posted == 2_500
    # Monday's balance is the same either way: the adjustment took effect after it.
    assert journal.balances(customer, MONDAY).posted == 3_000


def test_both_bounds_are_inclusive(journal: Journal, clock: ManualClock) -> None:
    clearing, customer = open_accounts(journal)
    clock.set(TUESDAY)

    journal.post(funding(clearing, customer, 3_000, effective_at=MONDAY))

    assert journal.balances(customer, MONDAY, known_at=TUESDAY).posted == 3_000
    just_before = timedelta(microseconds=1)
    assert journal.balances(customer, MONDAY - just_before, known_at=TUESDAY).posted == 0
    assert journal.balances(customer, MONDAY, known_at=TUESDAY - just_before).posted == 0


def test_a_balance_cannot_be_known_ahead_of_the_clock(journal: Journal) -> None:
    _, customer = open_accounts(journal)

    with pytest.raises(NotYetKnown):
        journal.balances(customer, MONDAY, known_at=TUESDAY)


def test_an_unknown_account_has_no_balance(journal: Journal) -> None:
    with pytest.raises(AccountNotFound):
        journal.balances("nobody", MONDAY)


def settlement(effective_at: datetime = TUESDAY, key: str = "") -> SettlementRequest:
    return SettlementRequest(
        idempotency_key=key or unique("settlement"), actor=ACH_JOB, effective_at=effective_at
    )


def test_a_pending_inflow_is_pending_until_it_settles(journal: Journal, clock: ManualClock) -> None:
    clearing, customer = open_accounts(journal)
    pending = journal.post(
        funding(clearing, customer, 3_000, status=TransactionStatus.PENDING)
    ).transaction
    clock.set(WEDNESDAY)

    settled = journal.settle(pending.id, settlement(effective_at=TUESDAY))

    assert pending.status is TransactionStatus.PENDING
    assert settled.transaction.settlement is not None
    assert settled.transaction.settlement.effective_at == TUESDAY
    # Monday: in motion. Money coming in isn't Available until it settles.
    monday = journal.balances(customer, MONDAY)
    assert (monday.posted, monday.pending, monday.available) == (0, 3_000, 0)
    # Tuesday, as known now: settled.
    tuesday = journal.balances(customer, TUESDAY)
    assert (tuesday.posted, tuesday.pending, tuesday.available) == (3_000, 0, 3_000)
    # Tuesday as known on Tuesday: the Ledger hadn't heard of the Settlement yet.
    then = journal.balances(customer, TUESDAY, known_at=TUESDAY)
    assert (then.posted, then.pending, then.available) == (0, 3_000, 0)


def payout(customer: str, clearing: str, amount: int, status: TransactionStatus) -> NewTransaction:
    """Money out for the Customer: debit the Customer Balance, credit Clearing."""
    return NewTransaction(
        idempotency_key=unique("payout"),
        actor=ACH_JOB,
        effective_at=MONDAY,
        status=status,
        entries=[
            NewEntry(account=customer, currency="USD", amount=amount),
            NewEntry(account=clearing, currency="USD", amount=-amount),
        ],
    )


def test_a_pending_outflow_reduces_available_but_not_posted(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    journal.post(funding(clearing, customer, 10_000))

    journal.post(payout(customer, clearing, 4_000, TransactionStatus.PENDING))

    balance = journal.balances(customer, MONDAY)
    assert balance.posted == 10_000
    assert balance.pending == -4_000
    assert balance.unsettled_outflows == 4_000
    assert balance.available == 6_000


def test_reversing_a_pending_transaction_cancels_it_while_pending(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    journal.post(funding(clearing, customer, 10_000))
    outflow = journal.post(payout(customer, clearing, 4_000, TransactionStatus.PENDING))

    reversal = journal.reverse(
        outflow.transaction.id,
        ReversalRequest(idempotency_key=unique("decline"), actor=ACH_JOB, effective_at=MONDAY),
    ).transaction

    assert reversal.status is TransactionStatus.PENDING
    balance = journal.balances(customer, MONDAY)
    assert (balance.posted, balance.pending, balance.available) == (10_000, 0, 10_000)
    with pytest.raises(AlreadyReversed):
        journal.settle(outflow.transaction.id, settlement())
    with pytest.raises(NotSettleable):
        journal.settle(reversal.id, settlement())


def test_a_return_of_a_settled_transaction_is_posted(journal: Journal, clock: ManualClock) -> None:
    clearing, customer = open_accounts(journal)
    pending = journal.post(funding(clearing, customer, 3_000, status=TransactionStatus.PENDING))
    journal.settle(pending.transaction.id, settlement(effective_at=TUESDAY))
    clock.set(WEDNESDAY)

    returned = journal.reverse(
        pending.transaction.id,
        ReversalRequest(idempotency_key=unique("return"), actor=ACH_JOB, effective_at=WEDNESDAY),
    ).transaction

    assert returned.status is TransactionStatus.POSTED
    assert returned.settlement is None
    monday = journal.balances(customer, MONDAY)
    tuesday = journal.balances(customer, TUESDAY)
    wednesday = journal.balances(customer, WEDNESDAY)
    assert (monday.posted, monday.pending, monday.available) == (0, 3_000, 0)
    assert (tuesday.posted, tuesday.pending, tuesday.available) == (3_000, 0, 3_000)
    assert (wednesday.posted, wednesday.pending, wednesday.available) == (0, 0, 0)


def test_a_return_cannot_take_effect_before_the_settlement(
    journal: Journal, clock: ManualClock
) -> None:
    clearing, customer = open_accounts(journal)
    pending = journal.post(funding(clearing, customer, 3_000, status=TransactionStatus.PENDING))
    journal.settle(pending.transaction.id, settlement(effective_at=WEDNESDAY))
    clock.set(WEDNESDAY)

    with pytest.raises(TooEarly):
        journal.reverse(
            pending.transaction.id,
            ReversalRequest(idempotency_key=unique("return"), actor=ACH_JOB, effective_at=TUESDAY),
        )

    assert journal.transaction(pending.transaction.id).reversed_by is None


@pytest.mark.parametrize("status", list(TransactionStatus))
def test_a_reversal_cannot_take_effect_before_its_original(
    journal: Journal, status: TransactionStatus
) -> None:
    clearing, customer = open_accounts(journal)
    original = journal.post(funding(clearing, customer, 3_000, effective_at=TUESDAY, status=status))

    with pytest.raises(TooEarly):
        journal.reverse(
            original.transaction.id,
            ReversalRequest(idempotency_key=unique("reverse"), actor=ACH_JOB, effective_at=MONDAY),
        )

    assert journal.transaction(original.transaction.id).reversed_by is None


def test_a_transaction_settles_once_and_retries_replay(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    pending = journal.post(funding(clearing, customer, 3_000, status=TransactionStatus.PENDING))
    request = settlement()
    first = journal.settle(pending.transaction.id, request)

    again = journal.settle(pending.transaction.id, request)

    assert (first.replayed, again.replayed) == (False, True)
    assert again.transaction == first.transaction
    with pytest.raises(AlreadySettled):
        journal.settle(pending.transaction.id, settlement())
    with pytest.raises(IdempotencyConflict):
        journal.settle(
            pending.transaction.id, settlement(effective_at=WEDNESDAY, key=request.idempotency_key)
        )


def test_only_a_pending_transaction_settles(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    posted = journal.post(funding(clearing, customer, 3_000))

    with pytest.raises(NotSettleable):
        journal.settle(posted.transaction.id, settlement())
    with pytest.raises(TransactionNotFound):
        journal.settle(2**62, settlement())


def test_a_settlement_cannot_take_effect_before_its_transaction(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    pending = journal.post(
        funding(clearing, customer, 3_000, effective_at=TUESDAY, status=TransactionStatus.PENDING)
    )

    with pytest.raises(TooEarly):
        journal.settle(pending.transaction.id, settlement(effective_at=MONDAY))
