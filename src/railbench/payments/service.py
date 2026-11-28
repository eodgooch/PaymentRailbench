"""Payments: originating them through a Provider, receiving Provider Events into the Inbox,
moving the state machine and booking each step through the Journal (SPEC §4, ADR 0010).

Receiving a webhook:

1. The Provider verifies its signature. An unsigned or stale webhook is refused and stored
   nowhere (it isn't a Provider Event, just a request).
2. The raw body and headers go into the Inbox, unique by (provider, event ID). A redelivery
   finds the row already there: that's the deduplication, before anything is applied.
3. The event is applied at most once: under an advisory lock on the Payment, and only if
   the Inbox row has no outcome yet. Applying records exactly one outcome: `applied`,
   `stale` (a state already passed) or `exception` (an impossible transition, with no
   Ledger write). An event for a Payment the Ledger hasn't recorded yet (its create call
   still in flight) gets no outcome and is refused with "retry later"; the redelivery
   applies it.

Booking an ACH pull, every Ledger write through the Journal, with the Provider Event's
Inbox ID as Actor:

- submitted: a pending Transaction, Clearing Account debit, Customer Balance credit.
- settled: a Settlement of that Transaction (Clearing and Customer Balance now Posted), a
  posted Transaction moving the money from the Clearing Account to Bank Cash (so Clearing
  nets to zero), and a Hold on the Customer Balance for the amount.
- canceled or declined before settlement: a Reversal of the pending Transaction, if any.
- available: `make_available()`, a Ledger-side job on the Clock, releases the Hold at the
  start of the second Banking Day after the settlement's Banking Day. The Provider never
  says when funds are Available; the Ledger decides from the calendar.

Applying one event is one database transaction: the Journal runs as a unit of work on the
same connection (`Journal.within`), so the Ledger writes, the state changes and the outcome
commit together or not at all. If the Ledger refuses any step, a savepoint rolls back every
Ledger write and state change of that event, and only the `exception` outcome is recorded.
Each Journal write still has an Idempotency Key derived from the Payment and the step.
"""

import logging
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, Row, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from railbench.common.calendar import add_banking_days, banking_day_starts, new_york_date
from railbench.common.clock import Clock
from railbench.common.fingerprint import fingerprint as payload_fingerprint
from railbench.ledger.journal import (
    AccountNotFound,
    DuplicateAccount,
    Journal,
    LedgerError,
)
from railbench.ledger.model import (
    AccountType,
    NewAccount,
    NewEntry,
    NewHold,
    NewTransaction,
    ProviderEventActor,
    ReleaseRequest,
    ReversalRequest,
    SettlementRequest,
    SystemJob,
    TransactionStatus,
)
from railbench.payments.model import (
    NewPayment,
    Outcome,
    Payment,
    PaymentCreated,
    PaymentException,
    Receipt,
    Transition,
)
from railbench.payments.provider import PaymentOrder, Provider, ProviderEvent
from railbench.payments.state_machine import (
    ENDS_BEFORE_SETTLEMENT,
    MAIN_PATH,
    AchState,
    Advance,
    Impossible,
    Stale,
    decide,
)
from railbench.payments.tables import inbox, inbox_outcomes, payments, transitions

log = logging.getLogger(__name__)

ORIGINATION_JOB = SystemJob(name="ach-origination", version="1.0.0")
AVAILABILITY_JOB = SystemJob(name="ach-availability", version="1.0.0")
AVAILABLE_AFTER_BANKING_DAYS = 2
# Payments' advisory locks use the two-key form, a separate space from the Journal's
# one-key locks on Transaction ids.
LOCK_SPACE = 4


class PaymentsError(Exception):
    """A request Payments refuses. Subclasses say why; the HTTP layer maps each to a status."""


class PaymentNotFound(PaymentsError):
    pass


class PaymentConflict(PaymentsError):
    """The Idempotency Key was already used for a different Payment."""


class NotACustomerBalance(PaymentsError):
    pass


class UnknownProvider(PaymentsError):
    pass


class PaymentNotYetKnown(PaymentsError):
    """A Provider Event for a Payment the Ledger hasn't recorded yet. Retry later."""


def clearing_account(provider: str, rail: str, currency: str) -> str:
    """The Clearing Account for one Provider, Rail and currency (SPEC §3.1)."""
    return f"clearing:{provider}:{rail}:{currency.lower()}"


def bank_cash_account(currency: str) -> str:
    return f"bank-cash:{currency.lower()}"


def available_at(settled_at: datetime) -> datetime:
    """When ACH money settled at `settled_at` becomes Available (ADR 0004)."""
    day = add_banking_days(new_york_date(settled_at), AVAILABLE_AFTER_BANKING_DAYS)
    return banking_day_starts(day)


@dataclass(frozen=True)
class _Booked:
    transaction_id: int | None = None
    hold_id: int | None = None


class Payments:
    def __init__(self, engine: Engine, clock: Clock, journal: Journal, provider: Provider) -> None:
        self._engine = engine
        self._clock = clock
        self._journal = journal
        self._provider = provider

    # Origination

    def originate(self, new: NewPayment) -> PaymentCreated:
        """Ask the Provider to make the Payment, once per Idempotency Key, and record it as
        `created`. The Provider is asked with the same Idempotency Key, so a retry after a
        failure part way never makes a second Payment."""
        provider = self._provider
        fingerprint = payload_fingerprint(new.model_dump(exclude={"idempotency_key"}))
        existing = self._by_key(new.idempotency_key, fingerprint)
        if existing is not None:
            return PaymentCreated(payment=existing, replayed=True)
        try:
            account = self._journal.account(new.customer_account)
        except AccountNotFound as error:
            raise NotACustomerBalance(f"no Account {new.customer_account}") from error
        if account.type is not AccountType.LIABILITY or account.currency != new.currency:
            raise NotACustomerBalance(
                f"{new.customer_account} is not a {new.currency} Customer Balance"
            )
        self._ensure_account(clearing_account(provider.name, new.rail, new.currency), new)
        self._ensure_account(bank_cash_account(new.currency), new)

        made = provider.create_payment(
            PaymentOrder(
                idempotency_key=new.idempotency_key,
                rail=new.rail,
                direction=new.direction,
                amount=new.amount,
                currency=new.currency,
                reference=new.description,
            )
        )
        now = self._clock.now()
        with self._engine.begin() as connection:
            payment_id = connection.scalar(
                pg_insert(payments)
                .values(
                    idempotency_key=new.idempotency_key,
                    payload_fingerprint=fingerprint,
                    provider=provider.name,
                    provider_payment_id=made.provider_payment_id,
                    rail=new.rail,
                    direction=new.direction,
                    customer_account=new.customer_account,
                    currency=new.currency,
                    amount=new.amount,
                    description=new.description,
                    recorded_at=now,
                )
                .on_conflict_do_nothing(index_elements=[payments.c.idempotency_key])
                .returning(payments.c.id)
            )
            if payment_id is not None:
                _append_transition(
                    connection, payment_id, AchState.CREATED, now, now, job=ORIGINATION_JOB
                )
        if payment_id is None:  # a concurrent request with this key got there first
            existing = self._by_key(new.idempotency_key, fingerprint)
            assert existing is not None
            return PaymentCreated(payment=existing, replayed=True)
        return PaymentCreated(payment=self.payment(payment_id), replayed=False)

    def payment(self, payment_id: int) -> Payment:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(payments).where(payments.c.id == payment_id)
            ).one_or_none()
            if row is None:
                raise PaymentNotFound(f"no Payment {payment_id}")
            steps = connection.execute(
                select(transitions)
                .where(transitions.c.payment_id == payment_id)
                .order_by(transitions.c.id)
            ).all()
            exceptions = connection.execute(
                select(inbox_outcomes)
                .where(
                    inbox_outcomes.c.payment_id == payment_id,
                    inbox_outcomes.c.outcome == Outcome.EXCEPTION,
                )
                .order_by(inbox_outcomes.c.id)
            ).all()
        states = [AchState(step.state) for step in steps]
        settled = next((s for s in steps if s.state == AchState.SETTLED), None)
        return Payment(
            id=row.id,
            idempotency_key=row.idempotency_key,
            provider=row.provider,
            provider_payment_id=row.provider_payment_id,
            rail=row.rail,
            direction=row.direction,
            customer_account=row.customer_account,
            currency=row.currency,
            amount=row.amount,
            description=row.description,
            recorded_at=row.recorded_at,
            state=_current(states),
            available_at=None if settled is None else available_at(settled.effective_at),
            transitions=[
                Transition(
                    state=AchState(step.state),
                    effective_at=step.effective_at,
                    recorded_at=step.recorded_at,
                    inbox_id=step.inbox_id,
                    job=None if step.job_name is None else f"{step.job_name}@{step.job_version}",
                    transaction_id=step.transaction_id,
                    hold_id=step.hold_id,
                )
                for step in steps
            ],
            exceptions=[
                PaymentException(inbox_id=e.inbox_id, detail=e.detail, recorded_at=e.recorded_at)
                for e in exceptions
            ],
        )

    # The Inbox

    def receive(self, provider_name: str, body: bytes, headers: Mapping[str, str]) -> Receipt:
        """Verify, store raw, deduplicate, then apply one webhook. Raises BadSignature,
        MalformedEvent or PaymentNotYetKnown."""
        provider = self._named(provider_name)
        provider.verify_event(body, headers, self._clock.now())
        return self._store_and_apply(provider, body, headers)

    def replay(self, provider_name: str, body: bytes, headers: Mapping[str, str]) -> Receipt:
        """Apply an Inbox body that was verified when it first arrived, without verifying it
        again (its signature's timestamp is long past). Replaying the Inbox in order into an
        empty Ledger reproduces the Ledger (SPEC §4.2). In-process only: no HTTP route
        reaches it."""
        return self._store_and_apply(self._named(provider_name), body, headers)

    def _store_and_apply(
        self, provider: Provider, body: bytes, headers: Mapping[str, str]
    ) -> Receipt:
        event = provider.read_event(body)
        with self._engine.begin() as connection:
            inbox_id = connection.scalar(
                pg_insert(inbox)
                .values(
                    provider=provider.name,
                    event_id=event.event_id,
                    body=body,
                    headers=dict(headers),
                    received_at=self._clock.now(),
                )
                .on_conflict_do_nothing(index_elements=[inbox.c.provider, inbox.c.event_id])
                .returning(inbox.c.id)
            )
            duplicate = inbox_id is None
            if duplicate:
                # Dedup by event ID: what's applied is the event as first stored, whatever
                # this delivery's body says.
                original = connection.execute(
                    select(inbox.c.id, inbox.c.body).where(
                        inbox.c.provider == provider.name, inbox.c.event_id == event.event_id
                    )
                ).one()
                inbox_id, event = original.id, provider.read_event(original.body)
        assert inbox_id is not None
        outcome, detail = self._apply(provider.name, inbox_id, event)
        return Receipt(
            inbox_id=inbox_id,
            event_id=event.event_id,
            duplicate=duplicate,
            outcome=outcome,
            detail=detail,
        )

    def _apply(self, provider: str, inbox_id: int, event: ProviderEvent) -> tuple[Outcome, str]:
        """Apply one Inbox event in one database transaction: the Ledger writes, the state
        changes and the outcome commit together or not at all."""
        with self._engine.begin() as connection:
            payment_id = connection.scalar(
                select(payments.c.id).where(
                    payments.c.provider == provider,
                    payments.c.provider_payment_id == event.provider_payment_id,
                )
            )
            if payment_id is None:
                raise PaymentNotYetKnown(
                    f"no Payment for {provider} {event.provider_payment_id} yet"
                )
            _lock(connection, payment_id)
            recorded = _outcome(connection, inbox_id)
            if recorded is not None:
                return recorded
            payment = connection.execute(select(payments).where(payments.c.id == payment_id)).one()
            decision = decide(_current(_states(connection, payment_id)), event.reported)
            outcome, detail = Outcome.APPLIED, ""
            match decision:
                case Stale():
                    outcome = Outcome.STALE
                case Impossible(reason=reason):
                    outcome, detail = Outcome.EXCEPTION, reason
                case Advance(path=path):
                    try:
                        # A savepoint: if the Ledger refuses any step, every Ledger write and
                        # state change of this event rolls back, and only the exception stays.
                        with connection.begin_nested():
                            journal = self._journal.within(connection)
                            for state in path:
                                booked = self._book(
                                    journal, connection, payment, state, inbox_id, event
                                )
                                _append_transition(
                                    connection,
                                    payment_id,
                                    state,
                                    event.occurred_at,
                                    self._clock.now(),
                                    inbox_id=inbox_id,
                                    booked=booked,
                                )
                    except LedgerError as error:
                        outcome, detail = Outcome.EXCEPTION, f"the Ledger refused it: {error}"
            if outcome is Outcome.EXCEPTION:
                log.warning(
                    "Provider Event not applied",
                    extra={"payment_id": payment_id, "inbox_id": inbox_id, "reason": detail},
                )
            _record_outcome(connection, payment_id, inbox_id, outcome, detail, self._clock.now())
        return outcome, detail

    def _book(
        self,
        journal: Journal,
        connection: Connection,
        payment: Row[Any],
        state: AchState,
        inbox_id: int,
        event: ProviderEvent,
    ) -> _Booked:
        actor = ProviderEventActor(inbox_id=str(inbox_id))
        key = f"payment:{payment.id}"
        clearing = clearing_account(payment.provider, payment.rail, payment.currency)
        customer = payment.customer_account

        def legs(debit: str, credit: str) -> list[NewEntry]:
            return [
                NewEntry(account=debit, currency=payment.currency, amount=payment.amount),
                NewEntry(account=credit, currency=payment.currency, amount=-payment.amount),
            ]

        match state:
            case AchState.SUBMITTED:
                pending = journal.post(
                    NewTransaction(
                        idempotency_key=f"{key}:pending",
                        actor=actor,
                        effective_at=event.occurred_at,
                        description=f"ACH pull {payment.provider_payment_id} submitted",
                        status=TransactionStatus.PENDING,
                        entries=legs(clearing, customer),
                    )
                ).transaction
                return _Booked(transaction_id=pending.id)
            case AchState.SETTLED:
                pending_id = _booked_by(connection, payment.id, AchState.SUBMITTED)
                assert pending_id is not None
                journal.settle(
                    pending_id,
                    SettlementRequest(
                        idempotency_key=f"{key}:settlement",
                        actor=actor,
                        effective_at=event.occurred_at,
                    ),
                )
                sweep = journal.post(
                    NewTransaction(
                        idempotency_key=f"{key}:bank-cash",
                        actor=actor,
                        effective_at=event.occurred_at,
                        description=f"ACH pull {payment.provider_payment_id} settled to Bank Cash",
                        entries=legs(bank_cash_account(payment.currency), clearing),
                    )
                ).transaction
                until = available_at(event.occurred_at)
                hold = journal.place_hold(
                    NewHold(
                        idempotency_key=f"{key}:availability-hold",
                        actor=actor,
                        effective_at=event.occurred_at,
                        account=customer,
                        currency=payment.currency,
                        amount=payment.amount,
                        description=f"ACH funds Available from {until.isoformat()}",
                    )
                ).hold
                return _Booked(transaction_id=sweep.id, hold_id=hold.id)
            case AchState.CANCELED | AchState.DECLINED:
                pending_id = _booked_by(connection, payment.id, AchState.SUBMITTED)
                if pending_id is None:
                    return _Booked()
                reversal = journal.reverse(
                    pending_id,
                    ReversalRequest(
                        idempotency_key=f"{key}:{state}",
                        actor=actor,
                        effective_at=event.occurred_at,
                        description=f"ACH pull {payment.provider_payment_id} {state}",
                    ),
                ).transaction
                return _Booked(transaction_id=reversal.id)
        raise AssertionError(f"a Provider Event can't book {state}")

    # Availability

    def make_available(self) -> int:
        """Release the availability Hold of every settled ACH Payment whose time has come,
        at its `available_at`, and record it `available`. Returns how many it moved."""
        now = self._clock.now()
        later = transitions.alias("later")
        with self._engine.connect() as connection:
            settled = connection.execute(
                select(transitions.c.payment_id, transitions.c.effective_at)
                .where(
                    transitions.c.state == AchState.SETTLED,
                    ~select(later.c.id)
                    .where(
                        later.c.payment_id == transitions.c.payment_id,
                        later.c.state == AchState.AVAILABLE,
                    )
                    .exists(),
                )
                .order_by(transitions.c.payment_id)
            ).all()
        moved = 0
        for payment_id, settled_at in settled:
            due = available_at(settled_at)
            if due <= now and self._make_available(payment_id, due):
                moved += 1
        return moved

    def _make_available(self, payment_id: int, due: datetime) -> bool:
        with self._engine.begin() as connection:
            _lock(connection, payment_id)
            if _current(_states(connection, payment_id)) is not AchState.SETTLED:
                return False
            hold_id: int = connection.execute(
                select(transitions.c.hold_id).where(
                    transitions.c.payment_id == payment_id,
                    transitions.c.state == AchState.SETTLED,
                )
            ).scalar_one()
            self._journal.within(connection).release_hold(
                hold_id,
                ReleaseRequest(
                    idempotency_key=f"payment:{payment_id}:available",
                    actor=AVAILABILITY_JOB,
                    effective_at=due,
                ),
            )
            _append_transition(
                connection,
                payment_id,
                AchState.AVAILABLE,
                due,
                self._clock.now(),
                job=AVAILABILITY_JOB,
                booked=_Booked(hold_id=hold_id),
            )
        return True

    # Helpers

    def _named(self, name: str) -> Provider:
        if name != self._provider.name:
            raise UnknownProvider(f"no Provider {name}")
        return self._provider

    def _by_key(self, key: str, fingerprint: str) -> Payment | None:
        with self._engine.connect() as connection:
            original = connection.execute(
                select(payments.c.id, payments.c.payload_fingerprint).where(
                    payments.c.idempotency_key == key
                )
            ).one_or_none()
        if original is None:
            return None
        if original.payload_fingerprint != fingerprint:
            raise PaymentConflict(f"Idempotency Key {key!r} was used for a different Payment")
        return self.payment(original.id)

    def _ensure_account(self, code: str, new: NewPayment) -> None:
        try:
            self._journal.account(code)
        except AccountNotFound:
            with suppress(DuplicateAccount):  # opened concurrently
                self._journal.open_account(
                    NewAccount(code=code, type=AccountType.ASSET, currency=new.currency)
                )


def _current(states: list[AchState]) -> AchState:
    ended = [state for state in states if state in ENDS_BEFORE_SETTLEMENT]
    if ended:
        return ended[0]
    return max(states, key=MAIN_PATH.index)


def _states(connection: Connection, payment_id: int) -> list[AchState]:
    return [
        AchState(state)
        for state in connection.scalars(
            select(transitions.c.state).where(transitions.c.payment_id == payment_id)
        )
    ]


def _booked_by(connection: Connection, payment_id: int, state: AchState) -> int | None:
    found: int | None = connection.scalar(
        select(transitions.c.transaction_id).where(
            transitions.c.payment_id == payment_id, transitions.c.state == state
        )
    )
    return found


def _outcome(connection: Connection, inbox_id: int) -> tuple[Outcome, str] | None:
    row = connection.execute(
        select(inbox_outcomes.c.outcome, inbox_outcomes.c.detail).where(
            inbox_outcomes.c.inbox_id == inbox_id
        )
    ).one_or_none()
    return None if row is None else (Outcome(row.outcome), row.detail)


def _record_outcome(
    connection: Connection,
    payment_id: int,
    inbox_id: int,
    outcome: Outcome,
    detail: str,
    recorded_at: datetime,
) -> None:
    connection.execute(
        insert(inbox_outcomes).values(
            inbox_id=inbox_id,
            payment_id=payment_id,
            outcome=outcome,
            detail=detail,
            recorded_at=recorded_at,
        )
    )


def _append_transition(
    connection: Connection,
    payment_id: int,
    state: AchState,
    effective_at: datetime,
    recorded_at: datetime,
    *,
    inbox_id: int | None = None,
    job: SystemJob | None = None,
    booked: _Booked = _Booked(),
) -> None:
    connection.execute(
        insert(transitions).values(
            payment_id=payment_id,
            state=state,
            inbox_id=inbox_id,
            job_name=None if job is None else job.name,
            job_version=None if job is None else job.version,
            transaction_id=booked.transaction_id,
            hold_id=booked.hold_id,
            effective_at=effective_at,
            recorded_at=recorded_at,
        )
    )


def _lock(connection: Connection, payment_id: int) -> None:
    """Serialise applying events to one Payment until this commit ends (an advisory lock:
    the app role has no UPDATE, so no row locks)."""
    connection.execute(select(func.pg_advisory_xact_lock(LOCK_SPACE, payment_id % 2_147_483_647)))
