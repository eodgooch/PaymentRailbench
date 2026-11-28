"""The Journal: posting Transactions to the Ledger, against a real Postgres."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from railbench.common.clock import ManualClock
from railbench.ledger.journal import (
    AlreadyReversed,
    CannotReverseReversal,
    CurrencyMismatch,
    DuplicateAccount,
    IdempotencyConflict,
    Journal,
    TransactionNotFound,
    UnknownAccount,
)
from railbench.ledger.model import (
    AccountType,
    NewAccount,
    NewEntry,
    NewTransaction,
    Person,
    ReversalRequest,
    SystemJob,
)
from tests.integration.helpers import posted_balance, unique

pytestmark = pytest.mark.integration

AUCTION_CLOSE = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(AUCTION_CLOSE)


@pytest.fixture
def journal(app_engine: Engine, clock: ManualClock) -> Journal:
    return Journal(app_engine, clock)


def test_a_new_account_has_a_zero_posted_balance(journal: Journal) -> None:
    code = unique("customer-balance")

    account = journal.open_account(
        NewAccount(code=code, type=AccountType.LIABILITY, currency="USD")
    )

    assert account.code == code
    assert account.type is AccountType.LIABILITY
    assert account.currency == "USD"
    assert posted_balance(journal, code) == 0


SETTLEMENT_JOB = SystemJob(name="ach-settlement", version="1.0.0")


def open_accounts(journal: Journal, currency: str = "USD") -> tuple[str, str]:
    """A Clearing Account and a Customer Balance in one currency."""
    clearing = journal.open_account(
        NewAccount(code=unique("clearing"), type=AccountType.ASSET, currency=currency)
    )
    customer = journal.open_account(
        NewAccount(code=unique("customer"), type=AccountType.LIABILITY, currency=currency)
    )
    return clearing.code, customer.code


def funding(clearing: str, customer: str, amount: int, key: str | None = None) -> NewTransaction:
    return NewTransaction(
        idempotency_key=key or unique("funding"),
        actor=SETTLEMENT_JOB,
        effective_at=AUCTION_CLOSE,
        entries=[
            NewEntry(account=clearing, currency="USD", amount=amount),
            NewEntry(account=customer, currency="USD", amount=-amount),
        ],
    )


def test_posting_a_balanced_transaction_moves_posted_balances(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)

    posted = journal.post(funding(clearing, customer, 12_500))

    assert posted.replayed is False
    # Balances read in each Account's normal direction (ADR 0009): the Clearing Account (an
    # asset) holds 12,500 and Mintwharf owes the Customer (a liability) 12,500.
    assert posted_balance(journal, clearing) == 12_500
    assert posted_balance(journal, customer) == 12_500
    assert journal.transaction(posted.transaction.id) == posted.transaction


def test_every_entry_records_its_actor_and_both_times(journal: Journal, clock: ManualClock) -> None:
    clearing, customer = open_accounts(journal)
    clock.advance(timedelta(minutes=5))

    posted = journal.post(funding(clearing, customer, 700))

    for entry in posted.transaction.entries:
        assert entry.actor == SETTLEMENT_JOB
        assert entry.effective_at == AUCTION_CLOSE
        assert entry.recorded_at == AUCTION_CLOSE + timedelta(minutes=5)


def test_reposting_the_same_key_and_payload_returns_the_original(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    request = funding(clearing, customer, 4_200)
    first = journal.post(request)

    again = journal.post(request)

    assert again.replayed is True
    assert again.transaction == first.transaction
    assert posted_balance(journal, customer) == 4_200


def test_the_same_effective_time_in_another_offset_is_the_same_payload(
    journal: Journal,
) -> None:
    clearing, customer = open_accounts(journal)
    request = funding(clearing, customer, 4_200)
    first = journal.post(request)
    eastern = timezone(timedelta(hours=-5))

    again = journal.post(
        request.model_copy(update={"effective_at": AUCTION_CLOSE.astimezone(eastern)})
    )

    assert again.transaction.id == first.transaction.id


def test_reusing_a_key_with_a_different_payload_is_a_conflict(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    key = unique("funding")
    journal.post(funding(clearing, customer, 4_200, key=key))

    with pytest.raises(IdempotencyConflict):
        journal.post(funding(clearing, customer, 4_201, key=key))

    assert posted_balance(journal, customer) == 4_200


def test_an_unbalanced_transaction_is_rejected_before_it_reaches_the_ledger() -> None:
    with pytest.raises(ValidationError, match="sum to zero"):
        NewTransaction(
            idempotency_key=unique("funding"),
            actor=SETTLEMENT_JOB,
            effective_at=AUCTION_CLOSE,
            entries=[
                NewEntry(account="clearing", currency="USD", amount=100),
                NewEntry(account="customer", currency="USD", amount=-99),
            ],
        )


def test_entries_balance_per_currency_not_in_total() -> None:
    with pytest.raises(ValidationError, match="sum to zero"):
        NewTransaction(
            idempotency_key=unique("conversion"),
            actor=SETTLEMENT_JOB,
            effective_at=AUCTION_CLOSE,
            entries=[
                NewEntry(account="usd-cash", currency="USD", amount=100),
                NewEntry(account="usdc-cash", currency="USDC", amount=-100),
            ],
        )


@pytest.mark.parametrize("amount", [10.0, "10", True, 0])
def test_an_amount_must_be_a_non_zero_integer(amount: object) -> None:
    with pytest.raises(ValidationError):
        NewEntry.model_validate({"account": "customer", "currency": "USD", "amount": amount})


def test_an_entry_to_an_unknown_account_is_rejected(journal: Journal) -> None:
    clearing, _ = open_accounts(journal)

    with pytest.raises(UnknownAccount):
        journal.post(funding(clearing, "nobody", 100))

    assert posted_balance(journal, clearing) == 0


def test_an_entry_must_be_in_its_accounts_currency(journal: Journal) -> None:
    clearing, _ = open_accounts(journal)
    _, usdc_customer = open_accounts(journal, currency="USDC")

    with pytest.raises(CurrencyMismatch):
        journal.post(funding(clearing, usdc_customer, 100))


def test_an_account_code_is_opened_once(journal: Journal) -> None:
    code = unique("fee-revenue")
    journal.open_account(NewAccount(code=code, type=AccountType.REVENUE, currency="USD"))

    with pytest.raises(DuplicateAccount):
        journal.open_account(NewAccount(code=code, type=AccountType.EXPENSE, currency="USD"))


OPERATOR = Person(operator_id="op-17", proposal_id="prop-204")


def reversal(key: str | None = None) -> ReversalRequest:
    return ReversalRequest(
        idempotency_key=key or unique("reversal"),
        actor=OPERATOR,
        effective_at=AUCTION_CLOSE + timedelta(days=1),
    )


def test_a_reversal_negates_every_entry_and_references_the_original(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    original = journal.post(funding(clearing, customer, 9_900)).transaction

    reversed_ = journal.reverse(original.id, reversal()).transaction

    assert reversed_.reverses == original.id
    assert journal.transaction(original.id).reversed_by == reversed_.id
    assert [(e.account, e.currency, e.amount) for e in reversed_.entries] == [
        (clearing, "USD", -9_900),
        (customer, "USD", 9_900),
    ]
    assert all(e.actor == OPERATOR for e in reversed_.entries)
    assert all(e.effective_at == AUCTION_CLOSE + timedelta(days=1) for e in reversed_.entries)
    assert posted_balance(journal, clearing) == 0
    assert posted_balance(journal, customer) == 0


def test_a_transaction_can_be_reversed_once(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    original = journal.post(funding(clearing, customer, 9_900)).transaction
    journal.reverse(original.id, reversal())

    with pytest.raises(AlreadyReversed):
        journal.reverse(original.id, reversal())

    assert posted_balance(journal, customer) == 0


def test_retrying_a_reversal_returns_the_original_reversal(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    original = journal.post(funding(clearing, customer, 9_900)).transaction
    request = reversal()
    first = journal.reverse(original.id, request)

    again = journal.reverse(original.id, request)

    assert again.replayed is True
    assert again.transaction == first.transaction


def test_a_reversal_cannot_itself_be_reversed(journal: Journal) -> None:
    clearing, customer = open_accounts(journal)
    original = journal.post(funding(clearing, customer, 9_900)).transaction
    reversed_ = journal.reverse(original.id, reversal()).transaction

    with pytest.raises(CannotReverseReversal):
        journal.reverse(reversed_.id, reversal())


def test_reversing_an_unknown_transaction_is_not_found(journal: Journal) -> None:
    with pytest.raises(TransactionNotFound):
        journal.reverse(2**62, reversal())
