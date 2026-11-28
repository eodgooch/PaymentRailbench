"""Replay a Ledger's Inbox from scratch into an empty database (SPEC §4.2, ADR 0011).

The Ledger's inputs are its own decisions (open an Account, originate a Payment), the
Provider Events in its Inbox, and its Clock (when the availability job ran). All three are
read back from the source database itself, nothing from the process that wrote it, and fed
in the same order, at the same Clock times, into a freshly migrated database: Accounts and
Payments as recorded (a stand-in Provider answers each origination with the Provider's ID
it gave the first time), every Inbox body applied through `Payments.replay` at the time it
was first received, and the availability job run at each moment it recorded doing
something. Because the Ledger reads time only from its Clock, Recorded Times come out the
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


class StepKind(IntEnum):
    """What the Ledger did, in the order it does them at one moment of a run."""

    OPEN_ACCOUNT = 0
    ORIGINATE = 1
    RECEIVE_EVENT = 2
    RUN_AVAILABILITY_JOB = 3


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
    replay to the Payments funding those Customer Balances, for a source shared with other
    work."""
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
        ],
        key=lambda step: step.sort_key,
    )
    if not steps:
        return
    clock = ManualClock(steps[0].at)
    journal = Journal(target, clock)
    service = Payments(target, clock, journal, provider)
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
