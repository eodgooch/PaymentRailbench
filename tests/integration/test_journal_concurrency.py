"""Idempotency under concurrency: simultaneous posts of one key post once, and a Transaction
is reversed, or a Hold ended, at most once however many requests race (SPEC §3.2, §3.3).

The Journal relies on the database's unique constraints, not on check-then-insert, so these
race real threads on real connections.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import count
from threading import Barrier

import pytest
from sqlalchemy import Engine

from railbench.common.clock import ManualClock
from railbench.ledger.journal import AlreadyReversed, HoldAlreadyEnded, Journal
from railbench.ledger.model import (
    AccountType,
    HoldRecorded,
    NewAccount,
    NewEntry,
    NewHold,
    NewTransaction,
    Person,
    Posted,
    ReleaseRequest,
    ReversalRequest,
    SystemJob,
)
from tests.integration.helpers import posted_balance, unique

pytestmark = pytest.mark.integration

AUCTION_CLOSE = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)
RACERS = 8


@pytest.fixture
def journal(app_engine: Engine) -> Journal:
    return Journal(app_engine, ManualClock(AUCTION_CLOSE))


def race[T](attempt: Callable[[], T]) -> list[T | Exception]:
    """Run `attempt` on RACERS threads released at the same instant."""
    start = Barrier(RACERS)

    def run() -> T | Exception:
        start.wait()
        try:
            return attempt()
        except Exception as error:
            return error

    with ThreadPoolExecutor(RACERS) as pool:
        return list(pool.map(lambda _: run(), range(RACERS)))


def funded(journal: Journal) -> tuple[str, NewTransaction]:
    clearing, customer = unique("clearing"), unique("customer")
    journal.open_account(NewAccount(code=clearing, type=AccountType.ASSET, currency="USD"))
    journal.open_account(NewAccount(code=customer, type=AccountType.LIABILITY, currency="USD"))
    return customer, NewTransaction(
        idempotency_key=unique("funding"),
        actor=SystemJob(name="ach-settlement", version="1.0.0"),
        effective_at=AUCTION_CLOSE,
        entries=[
            NewEntry(account=clearing, currency="USD", amount=2_500),
            NewEntry(account=customer, currency="USD", amount=-2_500),
        ],
    )


def test_simultaneous_posts_of_one_key_post_once(journal: Journal) -> None:
    customer, new = funded(journal)

    results = race(lambda: journal.post(new))

    posted = [result for result in results if isinstance(result, Posted)]
    assert len(posted) == RACERS, results
    assert sorted(result.replayed for result in posted) == [False] + [True] * (RACERS - 1)
    assert len({result.transaction.id for result in posted}) == 1
    assert posted_balance(journal, customer) == 2_500


def test_simultaneous_reversals_reverse_once(journal: Journal) -> None:
    customer, new = funded(journal)
    original = journal.post(new).transaction

    results = race(
        lambda: journal.reverse(
            original.id,
            ReversalRequest(
                idempotency_key=unique("reversal"),
                actor=Person(operator_id="op-17", proposal_id="prop-204"),
                effective_at=AUCTION_CLOSE,
            ),
        )
    )

    assert sum(isinstance(result, Posted) for result in results) == 1, results
    assert sum(isinstance(result, AlreadyReversed) for result in results) == RACERS - 1
    assert posted_balance(journal, customer) == 0


def test_racing_releases_and_captures_end_a_hold_once(journal: Journal) -> None:
    customer, new = funded(journal)
    journal.post(new)
    clearing = new.entries[0].account
    hold = journal.place_hold(
        NewHold(
            idempotency_key=unique("hold"),
            actor=SystemJob(name="auction-close", version="1.0.0"),
            effective_at=AUCTION_CLOSE,
            account=customer,
            currency="USD",
            amount=1_000,
        )
    ).hold
    turn = count()

    def end() -> Posted | HoldRecorded:
        if next(turn) % 2:
            return journal.release_hold(
                hold.id,
                ReleaseRequest(
                    idempotency_key=unique("release"),
                    actor=Person(operator_id="op-17", proposal_id="prop-204"),
                    effective_at=AUCTION_CLOSE,
                ),
            )
        return journal.capture_hold(
            hold.id,
            NewTransaction(
                idempotency_key=unique("capture"),
                actor=SystemJob(name="auction-close", version="1.0.0"),
                effective_at=AUCTION_CLOSE,
                entries=[
                    NewEntry(account=customer, currency="USD", amount=1_000),
                    NewEntry(account=clearing, currency="USD", amount=-1_000),
                ],
            ),
        )

    results = race(end)

    winners = [result for result in results if isinstance(result, Posted | HoldRecorded)]
    assert len(winners) == 1, results
    assert sum(isinstance(result, HoldAlreadyEnded) for result in results) == RACERS - 1
    captured = isinstance(winners[0], Posted)
    balance = journal.balance(customer, AUCTION_CLOSE)
    assert balance.held == 0
    assert balance.posted == (1_500 if captured else 2_500)
