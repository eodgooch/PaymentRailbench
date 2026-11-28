"""Replay a Ledger's Inbox from scratch into an empty database (SPEC §4.2, ADR 0011).

The Ledger's inputs are its own decisions (open an Account, originate a Payment), the
Provider Events in its Inbox, and its Clock (when the availability job ran). Replaying feeds
the same inputs, in the same order, at the same Clock times, into a freshly migrated
database: Accounts and Payments as recorded (a stand-in Provider answers each origination
with the Provider's ID it gave the first time), every Inbox body applied through
`Payments.replay` at the time it was first received, and the availability job run at every
original tick. Because the Ledger reads time only from its Clock, Recorded Times come out
the same too, and `ledger_summary` of the two databases must be equal.
"""

from collections.abc import Collection, Mapping
from contextlib import suppress
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select

from railbench.common.clock import ManualClock
from railbench.ledger.journal import Journal
from railbench.ledger.model import AccountType, NewAccount
from railbench.ledger.tables import accounts
from railbench.payments.model import NewPayment
from railbench.payments.provider import (
    PaymentOrder,
    ProviderEvent,
    ProviderPayment,
    SimulatorProvider,
)
from railbench.payments.service import PaymentNotYetKnown, Payments
from railbench.payments.state_machine import AchState
from railbench.payments.tables import inbox, payments

# At one moment: Accounts first, then originations, then Provider Events, then the job,
# which is the order a run does them in.
ACCOUNT, ORIGINATION, EVENT, TICK = range(4)


class RecordedProvider:
    """A Provider that answers from what the original Provider said: each origination gets
    the Provider's ID it got the first time. It reads events like the Simulator adapter and
    never verifies one (replayed bodies were verified when they first arrived)."""

    def __init__(self, name: str, provider_ids: Mapping[str, str]) -> None:
        self.name = name
        self._ids = provider_ids
        self._reader = SimulatorProvider("http://replay.invalid", "unused")

    def create_payment(self, order: PaymentOrder) -> ProviderPayment:
        return ProviderPayment(
            provider_payment_id=self._ids[order.idempotency_key], status=AchState.SUBMITTED
        )

    def get_payment(self, provider_payment_id: str) -> ProviderPayment:
        raise NotImplementedError("a replay never asks the Provider")

    def verify_event(self, body: bytes, headers: Mapping[str, str], now: datetime) -> None:
        raise NotImplementedError("a replay never verifies: the Inbox holds verified events")

    def read_event(self, body: bytes) -> ProviderEvent:
        return self._reader.read_event(body)


def replay_inbox(
    source: Engine,
    target: Engine,
    ticks: Collection[datetime],
    accounts_in_scope: Collection[str] | None = None,
) -> None:
    """Rebuild the Ledger in `source` (read as its owner) into the empty `target` (written
    as the Ledger's app role). `accounts_in_scope` limits the replay to the Payments funding
    those Customer Balances, for a source shared with other work."""
    with source.connect() as connection:
        account_rows = connection.execute(select(accounts).order_by(accounts.c.id)).all()
        payment_rows = connection.execute(select(payments).order_by(payments.c.id)).all()
        inbox_rows = connection.execute(select(inbox).order_by(inbox.c.id)).all()
    if accounts_in_scope is not None:
        account_rows = [a for a in account_rows if a.code in accounts_in_scope]
        payment_rows = [p for p in payment_rows if p.customer_account in accounts_in_scope]
    providers = {p.provider for p in payment_rows}
    if len(providers) > 1:
        raise NotImplementedError(f"replaying more than one Provider: {sorted(providers)}")
    provider = RecordedProvider(
        next(iter(providers), "simulator"),
        {p.idempotency_key: p.provider_payment_id for p in payment_rows},
    )
    ours = {p.provider_payment_id for p in payment_rows}
    inbox_rows = [
        row
        for row in inbox_rows
        if row.provider == provider.name
        and provider.read_event(row.body).provider_payment_id in ours
    ]

    timeline: list[tuple[datetime, int, int, Any]] = [
        *((a.recorded_at, ACCOUNT, a.id, a) for a in account_rows),
        *((p.recorded_at, ORIGINATION, p.id, p) for p in payment_rows),
        *((e.received_at, EVENT, e.id, e) for e in inbox_rows),
        *((moment, TICK, n, None) for n, moment in enumerate(ticks)),
    ]
    timeline.sort(key=lambda item: item[:3])
    if not timeline:
        return
    clock = ManualClock(timeline[0][0])
    journal = Journal(target, clock)
    service = Payments(target, clock, journal, provider)
    for moment, kind, _, row in timeline:
        clock.set(moment)
        if kind == ACCOUNT:
            journal.open_account(
                NewAccount(code=row.code, type=AccountType(row.type), currency=row.currency)
            )
        elif kind == ORIGINATION:
            service.originate(
                NewPayment(
                    idempotency_key=row.idempotency_key,
                    rail=row.rail,
                    direction=row.direction,
                    customer_account=row.customer_account,
                    amount=row.amount,
                    currency=row.currency,
                    description=row.description,
                )
            )
        elif kind == EVENT:
            with suppress(PaymentNotYetKnown):  # kept with no outcome, as the first time
                service.replay(provider.name, bytes(row.body), row.headers)
        else:
            service.make_available()
