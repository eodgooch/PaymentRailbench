"""Properties of the Journal under generated Transactions, against a real Postgres.

- every posted Transaction balances, and an unbalanced one leaves no trace
- an Idempotency Key posts once however often it is repeated
- a Reversal restores the Posted Balances from before the original
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from sqlalchemy import Engine

from railbench.common.clock import ManualClock
from railbench.ledger.journal import Journal
from railbench.ledger.model import (
    AccountType,
    NewAccount,
    NewEntry,
    NewTransaction,
    ReversalRequest,
    SystemJob,
)
from tests.integration.helpers import posted_balance, unique

pytestmark = pytest.mark.integration

AUCTION_CLOSE = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)
ACTOR = SystemJob(name="property-test", version="1")
CURRENCIES = ("USD", "USDC")
ACCOUNTS_PER_CURRENCY = 3

database = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@dataclass(frozen=True)
class Leg:
    currency: str
    account: int  # an index into that currency's Accounts
    amount: int


amounts = st.integers(min_value=-(10**15), max_value=10**15).filter(bool)


@st.composite
def balanced_legs(draw: st.DrawFn) -> list[Leg]:
    """Entries in one or both currencies that sum to zero in each."""
    legs: list[Leg] = []
    for currency in draw(
        st.lists(st.sampled_from(CURRENCIES), min_size=1, max_size=2, unique=True)
    ):
        indexes = st.integers(min_value=0, max_value=ACCOUNTS_PER_CURRENCY - 1)
        drawn = draw(st.lists(st.tuples(indexes, amounts), min_size=1, max_size=4))
        remainder = -sum(amount for _, amount in drawn)
        if remainder == 0:  # an Entry can't be zero, so add a unit to balance against
            drawn.append((draw(indexes), 1))
            remainder = -1
        legs += [Leg(currency, index, amount) for index, amount in drawn]
        legs.append(Leg(currency, draw(indexes), remainder))
    return legs


@pytest.fixture
def journal(app_engine: Engine) -> Journal:
    return Journal(app_engine, ManualClock(AUCTION_CLOSE))


def open_chart(journal: Journal) -> dict[str, list[str]]:
    """Fresh Accounts for one example, so examples don't see each other's Entries."""
    return {
        currency: [
            journal.open_account(
                NewAccount(code=unique("prop"), type=AccountType.ASSET, currency=currency)
            ).code
            for _ in range(ACCOUNTS_PER_CURRENCY)
        ]
        for currency in CURRENCIES
    }


def transaction(chart: dict[str, list[str]], legs: list[Leg], key: str = "") -> NewTransaction:
    return NewTransaction(
        idempotency_key=key or unique("prop"),
        actor=ACTOR,
        effective_at=AUCTION_CLOSE,
        entries=[
            NewEntry(
                account=chart[leg.currency][leg.account], currency=leg.currency, amount=leg.amount
            )
            for leg in legs
        ],
    )


def balances(journal: Journal, chart: dict[str, list[str]]) -> dict[str, int]:
    return {code: posted_balance(journal, code) for codes in chart.values() for code in codes}


@database
@given(legs=balanced_legs(), skew=st.one_of(st.just(0), amounts))
def test_every_posted_transaction_balances(journal: Journal, legs: list[Leg], skew: int) -> None:
    chart = open_chart(journal)
    first = legs[0]
    legs[0] = Leg(first.currency, first.account, first.amount + skew)

    try:
        new = transaction(chart, legs)
    except ValidationError:
        # Skewed off balance (or onto zero): nothing reaches the Ledger.
        assert skew != 0 or legs[0].amount == 0
        assert set(balances(journal, chart).values()) == {0}
        return
    posted = journal.post(new).transaction

    for currency, codes in chart.items():
        assert sum(posted_balance(journal, code) for code in codes) == 0, currency
    per_currency: defaultdict[str, int] = defaultdict(int)
    for entry in posted.entries:
        per_currency[entry.currency] += entry.amount
    assert set(per_currency.values()) == {0}
    assert len(posted.entries) == len(legs)


@database
@given(legs=balanced_legs(), repeats=st.integers(min_value=2, max_value=5))
def test_a_repeated_idempotency_key_posts_once(
    journal: Journal, legs: list[Leg], repeats: int
) -> None:
    chart = open_chart(journal)
    new = transaction(chart, legs)
    expected: defaultdict[str, int] = defaultdict(int)
    for entry in new.entries:
        expected[entry.account] += entry.amount

    results = [journal.post(new) for _ in range(repeats)]

    assert [result.replayed for result in results] == [False] + [True] * (repeats - 1)
    assert len({result.transaction.id for result in results}) == 1
    for code, balance in balances(journal, chart).items():
        assert balance == expected[code]


@database
@given(history=st.lists(balanced_legs(), max_size=3), target=balanced_legs())
def test_a_reversal_restores_prior_posted_balances(
    journal: Journal, history: list[list[Leg]], target: list[Leg]
) -> None:
    chart = open_chart(journal)
    for legs in history:
        journal.post(transaction(chart, legs))
    before = balances(journal, chart)
    original = journal.post(transaction(chart, target)).transaction

    journal.reverse(
        original.id,
        ReversalRequest(
            idempotency_key=unique("reversal"), actor=ACTOR, effective_at=AUCTION_CLOSE
        ),
    )

    assert balances(journal, chart) == before
