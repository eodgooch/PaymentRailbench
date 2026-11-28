"""A Return never leaves a Customer Balance negative (SPEC §4.3, §10, ADR 0013).

The invariant "a negative Customer Balance always has a matching Recovery Receivable", under
random amounts and timing: a Customer funds by two ACH pulls, spends any amount (perhaps more
than they have, as an Auction Close lets them), and one pull is returned at a random time
after it settled, before or after the Ledger made it Available. Whatever the order, the
Return reverses exactly its own funding, the Customer Balance ends at what's left or at
zero, Recovery Receivable holds exactly the deficit, and one return exception carries it.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine

from railbench.common.clock import ManualClock
from railbench.common.db import LEDGER_APP_ROLE, make_engine
from railbench.common.signing import SIGNATURE_HEADER, sign
from railbench.ledger.journal import AccountNotFound, Journal
from railbench.ledger.model import AccountType, NewAccount, NewEntry, NewTransaction, SystemJob
from railbench.payments.model import NewPayment
from railbench.payments.provider import SimulatorProvider
from railbench.payments.service import Payments, recovery_receivable_account
from railbench.scenarios.runner import fresh_database
from tests.integration.helpers import END_OF_TIME

pytestmark = pytest.mark.integration

SECRET = "return-property-secret"
START = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)  # a Tuesday morning in New York
SETTLES_AFTER = timedelta(hours=22, minutes=30)  # Wednesday 08:30 New York
SPENDER = SystemJob(name="auction-close-stand-in", version="1")


@pytest.fixture(scope="module")
def ledger(database_url: str) -> Iterator[Engine]:
    """A database of its own: the availability job acts on every Payment in it, and each
    example's Clock starts after the last one ended."""
    with fresh_database(database_url, "returns") as url:
        engine = make_engine(url, role=LEDGER_APP_ROLE)
        yield engine
        engine.dispose()


def provider() -> SimulatorProvider:
    def created(request: httpx.Request) -> httpx.Response:
        key = json.loads(request.content)["idempotency_key"]
        return httpx.Response(201, json={"id": f"simpay_{key}", "status": "submitted"})

    return SimulatorProvider(
        "http://simulator.invalid",
        SECRET,
        http=httpx.Client(
            base_url="http://simulator.invalid", transport=httpx.MockTransport(created)
        ),
    )


starts = [START]


@settings(
    max_examples=25,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    returned_amount=st.integers(min_value=1, max_value=10**9),
    other_amount=st.integers(min_value=1, max_value=10**9),
    spent=st.integers(min_value=0, max_value=3 * 10**9),
    returned_after=st.integers(min_value=0, max_value=60 * 24 * 3600),
    availability_first=st.booleans(),
)
def test_a_return_moves_exactly_the_deficit_to_recovery_receivable(
    ledger: Engine,
    returned_amount: int,
    other_amount: int,
    spent: int,
    returned_after: int,
    availability_first: bool,
) -> None:
    clock = ManualClock(starts[0])
    starts[0] += timedelta(days=120)
    journal = Journal(ledger, clock)
    payments = Payments(ledger, clock, journal, provider())
    run = uuid4().hex[:10]
    customer = f"customer:prop-{run}:usd"
    journal.open_account(NewAccount(code=customer, type=AccountType.LIABILITY, currency="USD"))

    def deliver(payment_key: str, type_: str, occurred_at: datetime) -> None:
        body = json.dumps(
            {
                "event_id": f"evt-{payment_key}-{type_}",
                "type": f"payment.{type_}",
                "occurred_at": occurred_at.isoformat(),
                "payment": {"id": f"simpay_{payment_key}"},
                **({"return": {"code": "R10"}} if type_ == "returned" else {}),
            }
        ).encode()
        receipt = payments.receive(
            "simulator", body, {SIGNATURE_HEADER: sign(SECRET, body, clock.now())}
        )
        assert receipt.outcome == "applied", receipt.detail

    pulls = {}
    for name, amount in (("returned", returned_amount), ("other", other_amount)):
        key = f"prop-{run}-{name}"
        pulls[name] = payments.originate(
            NewPayment(
                idempotency_key=key,
                rail="ach",
                direction="pull",
                customer_account=customer,
                amount=amount,
                currency="USD",
            )
        ).payment
        deliver(key, "submitted", clock.now())
    settled_at = clock.now() + SETTLES_AFTER
    clock.set(settled_at)
    for name in pulls:
        deliver(f"prop-{run}-{name}", "settled", settled_at)
    if spent:
        seller = f"customer:prop-{run}-seller:usd"
        journal.open_account(NewAccount(code=seller, type=AccountType.LIABILITY, currency="USD"))
        journal.post(
            NewTransaction(
                idempotency_key=f"prop-{run}-purchase",
                actor=SPENDER,
                effective_at=settled_at,
                entries=[
                    NewEntry(account=customer, currency="USD", amount=spent),
                    NewEntry(account=seller, currency="USD", amount=-spent),
                ],
            )
        )

    returned_at = settled_at + timedelta(seconds=returned_after)
    available_at = payments.payment(pulls["returned"].id).available_at
    assert available_at is not None  # settled, so the Ledger knows when it's Available
    if availability_first:
        clock.set(max(returned_at, available_at))
        payments.make_available()
    clock.set(max(clock.now(), returned_at))
    deliver(f"prop-{run}-returned", "returned", returned_at)
    payments.make_available()

    deficit = max(0, spent - other_amount)
    assert journal.balances(customer, END_OF_TIME).posted == max(0, other_amount - spent)
    try:
        owed = journal.balances(recovery_receivable_account(customer), END_OF_TIME).posted
    except AccountNotFound:
        owed = 0
    assert owed == deficit
    raised = payments.payment(pulls["returned"].id).return_exceptions
    assert [e.exposure for e in raised] == ([deficit] if deficit else [])
    if raised:  # a Late Return by time, whichever the Ledger did first
        assert raised[0].late == (returned_at > available_at)
    assert payments.payment(pulls["other"].id).return_exceptions == []
