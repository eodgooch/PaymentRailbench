"""Replay a Ledger's Inbox from scratch into an empty database (SPEC §4.2, ADR 0011).

The Ledger's inputs are its own decisions (open an Account, originate a Payment, close an
auction), the Provider Events in its Inbox, and its Clock (when its jobs ran). All three
are read back from the source database itself, nothing from the process that wrote it, and
fed in the same order, at the same Clock times, into a freshly migrated database: Accounts
and Payments as recorded (a stand-in Provider answers each origination with the Provider's
ID it gave the first time), each Auction Close from its lots and Payout requests with the fee
and Instant Payout limit it was booked under, every Inbox body applied through
`Payments.replay` at the time it was first received, and the availability and Payout
release jobs run at each moment they recorded doing something. At one moment they go in the
order the Ledger does them: Accounts, originations, Auction Closes, Provider Events, then
the two jobs. Because the Ledger reads time only from its Clock, Recorded Times come out the
same too, and `ledger_summary` of the two databases must be equal.
"""

from collections.abc import Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any

from sqlalchemy import Connection, Engine, select

from railbench.common.clock import ManualClock
from railbench.ledger.journal import Journal
from railbench.ledger.model import AccountType, NewAccount
from railbench.ledger.tables import accounts
from railbench.marketplace.model import NewAuctionClose, NewLot
from railbench.marketplace.service import PAYOUT_JOB, Marketplace
from railbench.marketplace.tables import auction_closes, lots, payout_steps, payouts
from railbench.payments.model import NewPayment
from railbench.payments.provider import (
    PaymentOrder,
    ProviderEvent,
    ProviderPayment,
    SimulatorProvider,
)
from railbench.payments.service import AVAILABILITY_JOB, PaymentNotYetKnown, Payments
from railbench.payments.state_machine import PaymentState
from railbench.payments.tables import inbox, payments, transitions


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
            provider_payment_id=self._ids[order.idempotency_key], status=PaymentState.SUBMITTED
        )

    def get_payment(self, provider_payment_id: str) -> ProviderPayment:
        raise NotImplementedError("a replay never asks the Provider")

    def verify_event(self, body: bytes, headers: Mapping[str, str], now: datetime) -> None:
        raise NotImplementedError("a replay never verifies: the Inbox holds verified events")

    def read_event(self, body: bytes) -> ProviderEvent:
        return self._reader.read_event(body)


def availability_job_runs(
    connection: Connection, payment_ids: Collection[int] | None = None
) -> list[datetime]:
    """When the Ledger's availability job did something, read from what it recorded: each
    `available` state change it appended, at its Recorded Time (the job's Clock reading).
    A run that released nothing wrote nothing, and replaying it would release nothing, so
    these are all the runs a replay needs."""
    query = select(transitions.c.recorded_at).where(
        transitions.c.job_name == AVAILABILITY_JOB.name,
        transitions.c.state == PaymentState.AVAILABLE,
    )
    if payment_ids is not None:
        query = query.where(transitions.c.payment_id.in_(payment_ids))
    return sorted(set(connection.scalars(query)))


def payout_job_runs(connection: Connection) -> list[datetime]:
    """When the Payout release job did something: each step it appended, at its Recorded
    Time. (A run that only sent a Payment the Provider refused earlier wrote no step; that
    Payment is replayed as an origination.)"""
    query = select(payout_steps.c.recorded_at).where(payout_steps.c.job_name == PAYOUT_JOB.name)
    return sorted(set(connection.scalars(query)))


class StepKind(IntEnum):
    """What the Ledger did, in the order it does them at one moment of a run."""

    OPEN_ACCOUNT = 0
    ORIGINATE = 1
    CLOSE_AUCTION = 2
    RECEIVE_EVENT = 3
    RUN_AVAILABILITY_JOB = 4
    RUN_PAYOUT_JOB = 5


@dataclass(frozen=True)
class Step:
    at: datetime
    kind: StepKind
    order: int  # the source row's id, to keep the original order within a moment
    row: Any = field(default=None, compare=False)

    @property
    def sort_key(self) -> tuple[datetime, int, int]:
        return (self.at, self.kind, self.order)


def replay_inbox(
    source: Engine, target: Engine, accounts_in_scope: Collection[str] | None = None
) -> None:
    """Rebuild the Ledger in `source` (read as its owner) into the empty `target` (written
    as the Ledger's app role), from the source database alone. `accounts_in_scope` limits the
    replay to the Payments funding those Customer Balances, and leaves Auction Closes out, for
    a source shared with other work."""
    with source.connect() as connection:
        account_rows = connection.execute(select(accounts).order_by(accounts.c.id)).all()
        payment_rows = connection.execute(select(payments).order_by(payments.c.id)).all()
        inbox_rows = connection.execute(select(inbox).order_by(inbox.c.id)).all()
        if accounts_in_scope is not None:
            account_rows = [a for a in account_rows if a.code in accounts_in_scope]
            payment_rows = [p for p in payment_rows if p.customer_account in accounts_in_scope]
        job_runs = availability_job_runs(
            connection, None if accounts_in_scope is None else [p.id for p in payment_rows]
        )
        closes: list[tuple[Any, NewAuctionClose]] = []
        payout_runs: list[datetime] = []
        if accounts_in_scope is None:
            closes = _auction_closes(connection)
            payout_runs = payout_job_runs(connection)
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

    steps = sorted(
        [
            *(Step(a.recorded_at, StepKind.OPEN_ACCOUNT, a.id, a) for a in account_rows),
            *(Step(p.recorded_at, StepKind.ORIGINATE, p.id, p) for p in payment_rows),
            *(Step(e.received_at, StepKind.RECEIVE_EVENT, e.id, e) for e in inbox_rows),
            *(Step(at, StepKind.RUN_AVAILABILITY_JOB, n) for n, at in enumerate(job_runs)),
            *(Step(c.recorded_at, StepKind.CLOSE_AUCTION, c.id, (c, new)) for c, new in closes),
            *(Step(at, StepKind.RUN_PAYOUT_JOB, n) for n, at in enumerate(payout_runs)),
        ],
        key=lambda step: step.sort_key,
    )
    if not steps:
        return
    clock = ManualClock(steps[0].at)
    journal = Journal(target, clock)
    service = Payments(target, clock, journal, provider)

    def marketplace(fee_bps: int = 0, limit: int = 0) -> Marketplace:
        return Marketplace(target, clock, journal, service, fee_bps, limit)

    for step in steps:
        clock.set(step.at)
        row = step.row
        match step.kind:
            case StepKind.OPEN_ACCOUNT:
                journal.open_account(
                    NewAccount(code=row.code, type=AccountType(row.type), currency=row.currency)
                )
            case StepKind.ORIGINATE:
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
            case StepKind.RECEIVE_EVENT:
                with suppress(PaymentNotYetKnown):  # kept with no outcome, as the first time
                    service.replay(provider.name, bytes(row.body), row.headers)
            case StepKind.RUN_AVAILABILITY_JOB:
                service.make_available()
            case StepKind.CLOSE_AUCTION:
                close, new = row
                marketplace(close.seller_fee_bps, close.instant_payout_limit).close(new)
            case StepKind.RUN_PAYOUT_JOB:
                marketplace().release_payouts()


def _auction_closes(connection: Connection) -> list[tuple[Any, NewAuctionClose]]:
    """Each Auction Close as the Ledger was asked for it: its lots in order and the sellers
    who asked for an Instant Payout."""
    found = []
    for close in connection.execute(select(auction_closes).order_by(auction_closes.c.id)):
        its_lots = connection.execute(
            select(lots).where(lots.c.auction_close_id == close.id).order_by(lots.c.id)
        ).all()
        instant = connection.scalars(
            select(payouts.c.customer_account).where(
                payouts.c.auction_close_id == close.id, payouts.c.requested == "instant"
            )
        ).all()
        new = NewAuctionClose(
            auction_id=close.auction_id,
            closed_at=close.closed_at,
            currency=close.currency,
            lots=[
                NewLot(
                    lot_id=lot.lot_id,
                    buyer=lot.buyer,
                    seller=lot.seller,
                    hammer_price=lot.hammer_price,
                )
                for lot in its_lots
            ],
            instant_payouts=list(instant),
        )
        found.append((close, new))
    return found
