"""Holds (SPEC §3.3, ADR 0009): placed, then released or captured once; they reduce the
Available Balance without moving money."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from railbench.common.clock import ManualClock
from railbench.ledger.journal import (
    CaptureMissesHold,
    CurrencyMismatch,
    HoldAlreadyEnded,
    HoldNotFound,
    IdempotencyConflict,
    Journal,
    TooEarly,
    UnknownAccount,
)
from railbench.ledger.model import (
    AccountType,
    HoldOutcome,
    NewAccount,
    NewEntry,
    NewHold,
    NewTransaction,
    Person,
    ReleaseRequest,
    SystemJob,
)
from tests.integration.helpers import unique

pytestmark = pytest.mark.integration

AUCTION_CLOSE = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)
LATER = AUCTION_CLOSE + timedelta(hours=2)
AUCTION_JOB = SystemJob(name="auction-close", version="1.0.0")
OPERATOR = Person(operator_id="op-17", proposal_id="prop-204")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(AUCTION_CLOSE)


@pytest.fixture
def journal(app_engine: Engine, clock: ManualClock) -> Journal:
    return Journal(app_engine, clock)


def funded_customer(journal: Journal, amount: int = 10_000) -> tuple[str, str]:
    """A Customer Balance holding `amount`, and the Fee Revenue Account to capture into."""
    cash, customer, fees = unique("bank-cash"), unique("customer"), unique("fee-revenue")
    journal.open_account(NewAccount(code=cash, type=AccountType.ASSET, currency="USD"))
    journal.open_account(NewAccount(code=customer, type=AccountType.LIABILITY, currency="USD"))
    journal.open_account(NewAccount(code=fees, type=AccountType.REVENUE, currency="USD"))
    journal.post(
        NewTransaction(
            idempotency_key=unique("funding"),
            actor=AUCTION_JOB,
            effective_at=AUCTION_CLOSE,
            entries=[
                NewEntry(account=cash, currency="USD", amount=amount),
                NewEntry(account=customer, currency="USD", amount=-amount),
            ],
        )
    )
    return customer, fees


def hold(account: str, amount: int = 2_500, key: str = "") -> NewHold:
    return NewHold(
        idempotency_key=key or unique("hold"),
        actor=AUCTION_JOB,
        effective_at=AUCTION_CLOSE,
        account=account,
        currency="USD",
        amount=amount,
        description="Winning bid on lot 42",
    )


def release(effective_at: datetime = LATER, key: str = "") -> ReleaseRequest:
    return ReleaseRequest(
        idempotency_key=key or unique("release"), actor=OPERATOR, effective_at=effective_at
    )


def charge(customer: str, fees: str, amount: int = 2_500, key: str = "") -> NewTransaction:
    """Captures a Hold by moving the money: debit the Customer Balance, credit Fee Revenue."""
    return NewTransaction(
        idempotency_key=key or unique("capture"),
        actor=AUCTION_JOB,
        effective_at=LATER,
        entries=[
            NewEntry(account=customer, currency="USD", amount=amount),
            NewEntry(account=fees, currency="USD", amount=-amount),
        ],
    )


def test_a_hold_reduces_available_without_moving_money(journal: Journal) -> None:
    customer, _ = funded_customer(journal)

    placed = journal.place_hold(hold(customer, 2_500))

    assert placed.replayed is False
    assert placed.hold.ended is None
    balance = journal.balance(customer, AUCTION_CLOSE)
    assert (balance.posted, balance.held, balance.available) == (10_000, 2_500, 7_500)


def test_placing_a_hold_is_idempotent(journal: Journal) -> None:
    customer, _ = funded_customer(journal)
    request = hold(customer)
    first = journal.place_hold(request)

    again = journal.place_hold(request)

    assert again.replayed is True
    assert again.hold == first.hold
    assert journal.balance(customer, AUCTION_CLOSE).held == 2_500
    with pytest.raises(IdempotencyConflict):
        journal.place_hold(hold(customer, 2_501, key=request.idempotency_key))


def test_releasing_a_hold_restores_available_from_the_release(journal: Journal) -> None:
    customer, _ = funded_customer(journal)
    placed = journal.place_hold(hold(customer))

    released = journal.release_hold(placed.hold.id, release(effective_at=LATER))

    assert released.hold.ended is not None
    assert released.hold.ended.outcome is HoldOutcome.RELEASED
    assert released.hold.ended.actor == OPERATOR
    assert journal.balance(customer, AUCTION_CLOSE).available == 7_500
    after = journal.balance(customer, LATER)
    assert (after.posted, after.held, after.available) == (10_000, 0, 10_000)


def test_capturing_a_hold_moves_the_money_and_ends_the_hold(journal: Journal) -> None:
    customer, fees = funded_customer(journal)
    placed = journal.place_hold(hold(customer, 2_500))

    captured = journal.capture_hold(placed.hold.id, charge(customer, fees, 2_000))

    ended = journal.hold(placed.hold.id).ended
    assert ended is not None
    assert ended.outcome is HoldOutcome.CAPTURED
    assert ended.transaction == captured.transaction.id
    assert ended.effective_at == LATER
    before = journal.balance(customer, AUCTION_CLOSE)
    after = journal.balance(customer, LATER)
    assert (before.posted, before.held, before.available) == (10_000, 2_500, 7_500)
    assert (after.posted, after.held, after.available) == (8_000, 0, 8_000)
    assert journal.balance(fees, LATER).posted == 2_000


def test_retrying_a_release_or_capture_replays(journal: Journal) -> None:
    customer, fees = funded_customer(journal)
    released, captured = journal.place_hold(hold(customer)), journal.place_hold(hold(customer))
    release_request, capture_request = release(), charge(customer, fees)
    first_release = journal.release_hold(released.hold.id, release_request)
    first_capture = journal.capture_hold(captured.hold.id, capture_request)

    again_release = journal.release_hold(released.hold.id, release_request)
    again_capture = journal.capture_hold(captured.hold.id, capture_request)

    assert again_release.replayed and again_capture.replayed
    assert again_release.hold == first_release.hold
    assert again_capture.transaction == first_capture.transaction
    assert journal.balance(customer, LATER).posted == 7_500


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("release", "release"),
        ("capture", "capture"),
        ("capture", "release"),
        ("release", "capture"),
    ],
)
def test_a_hold_ends_once(journal: Journal, first: str, second: str) -> None:
    customer, fees = funded_customer(journal)
    hold_id = journal.place_hold(hold(customer)).hold.id

    def end(how: str) -> None:
        if how == "release":
            journal.release_hold(hold_id, release())
        else:
            journal.capture_hold(hold_id, charge(customer, fees))

    end(first)
    before = journal.balance(customer, LATER)
    with pytest.raises(HoldAlreadyEnded):
        end(second)

    assert journal.balance(customer, LATER) == before


def test_a_capture_must_move_money_on_the_held_account(journal: Journal) -> None:
    customer, fees = funded_customer(journal)
    other, _ = funded_customer(journal)
    hold_id = journal.place_hold(hold(customer)).hold.id

    with pytest.raises(CaptureMissesHold):
        journal.capture_hold(hold_id, charge(other, fees))

    assert journal.hold(hold_id).ended is None


def test_a_hold_cannot_end_before_it_was_placed(journal: Journal) -> None:
    customer, fees = funded_customer(journal)
    hold_id = journal.place_hold(hold(customer)).hold.id
    too_early = AUCTION_CLOSE - timedelta(seconds=1)

    with pytest.raises(TooEarly):
        journal.release_hold(hold_id, release(effective_at=too_early))
    with pytest.raises(TooEarly):
        journal.capture_hold(
            hold_id, charge(customer, fees).model_copy(update={"effective_at": too_early})
        )


def test_an_unknown_hold_is_not_found(journal: Journal) -> None:
    with pytest.raises(HoldNotFound):
        journal.release_hold(2**62, release())
    with pytest.raises(HoldNotFound):
        journal.hold(2**62)


def test_a_hold_is_on_a_known_account_in_its_currency(journal: Journal) -> None:
    customer, _ = funded_customer(journal)

    with pytest.raises(UnknownAccount):
        journal.place_hold(hold("nobody"))
    with pytest.raises(CurrencyMismatch):
        journal.place_hold(hold(customer).model_copy(update={"currency": "USDC"}))


@pytest.mark.parametrize("amount", [0, -1, 1.0, "100", True], ids=repr)
def test_a_hold_amount_is_a_positive_integer(amount: object) -> None:
    with pytest.raises(ValidationError):
        NewHold.model_validate({**hold("customer").model_dump(), "amount": amount})


def test_holds_are_bitemporal(journal: Journal, clock: ManualClock) -> None:
    customer, _ = funded_customer(journal)
    clock.set(LATER)

    # Placed at LATER, backdated to the Auction Close.
    journal.place_hold(hold(customer))

    assert journal.balance(customer, AUCTION_CLOSE, known_at=AUCTION_CLOSE).held == 0
    assert journal.balance(customer, AUCTION_CLOSE).held == 2_500
