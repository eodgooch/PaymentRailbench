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

Booking, every Ledger write through the Journal, with the Provider Event's Inbox ID as
Actor. An ACH pull:

- submitted: a pending Transaction, Clearing Account debit, Customer Balance credit.
- settled: a Settlement of that Transaction (Clearing and Customer Balance now Posted), a
  posted Transaction moving the money from the Clearing Account to Bank Cash (so Clearing
  nets to zero), and a Hold on the Customer Balance for the amount.
- canceled or declined before settlement: a Reversal of the pending Transaction, if any.
- available: `make_available()`, a Ledger-side job on the Clock, releases the Hold at the
  start of the second Banking Day after the settlement's Banking Day. The Provider never
  says when funds are Available; the Ledger decides from the calendar.
- returned (from settled or available, with a NACHA return code; ADR 0013): a posted
  Reversal of the pending Transaction (Customer Balance debit, Clearing Account credit) and
  a posted Reversal of the settlement's sweep (Clearing Account debit, Bank Cash credit), so
  the money leaves Bank Cash and the Clearing Account still nets to zero; the availability
  Hold is released if it's still in place, so the money is never made Available. Then,
  from the Return's Effective Time on, wherever the Customer Balance is short (its Posted
  Balance less unsettled outflows below zero, as known now, because the money was spent), a
  posted Transaction at that moment moves the shortfall to the Customer's Recovery
  Receivable (Recovery Receivable debit, Customer Balance credit), each with a return
  exception recording the exposure. A Late Return is one after the funds became Available,
  by time.

An ACH push (a Payout) is the mirror image, and is never made Available:

- submitted: a pending Transaction, Customer Balance debit, Clearing Account credit, so the
  money is an unsettled outflow: still Posted, no longer Available. If the push names the
  Hold reserving its amount, this Transaction captures it.
- settled: its Settlement, and a posted Transaction from Bank Cash to the Clearing Account
  (the Clearing Account nets to zero; Bank Cash goes down).
- canceled or declined before settlement: a pending Reversal, so the money is Available in
  the Customer Balance again.

An RTP push is instant and final, `created → settled` or `declined`:

- settled: a posted Transaction, Customer Balance debit, RTP Clearing Account credit (it
  captures the push's reserving Hold, if any), and a posted Transaction from Bank Cash to
  that Clearing Account, which nets to zero at once.
- declined: nothing, since nothing was booked.

Applying one event is one database transaction: the Journal runs as a unit of work on the
same connection (`Journal.within`), so the Ledger writes, the state changes and the outcome
commit together or not at all. If the Ledger refuses any step, a savepoint rolls back every
Ledger write and state change of that event, and only the `exception` outcome is recorded.
Each Journal write still has an Idempotency Key derived from the Payment and the step.
"""

import logging
import re
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, Row, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from railbench.common.calendar import add_banking_days, banking_day_starts, new_york_date
from railbench.common.clock import Clock
from railbench.common.fingerprint import fingerprint as payload_fingerprint
from railbench.common.locks import LockSpace, lock, lock_name
from railbench.common.returns import ReturnCode, is_late
from railbench.ledger.journal import (
    AccountNotFound,
    DuplicateAccount,
    HoldNotFound,
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
    PaymentRef,
    Receipt,
    ReturnException,
    Transition,
)
from railbench.payments.provider import PaymentOrder, Provider, ProviderEvent
from railbench.payments.state_machine import (
    ENDS_BEFORE_SETTLEMENT,
    MAIN_PATH,
    Advance,
    Impossible,
    PaymentState,
    Stale,
    decide,
)
from railbench.payments.tables import (
    inbox,
    inbox_outcomes,
    payments,
    return_exceptions,
    transitions,
)

log = logging.getLogger(__name__)

ORIGINATION_JOB = SystemJob(name="payment-origination", version="1.0.0")
AVAILABILITY_JOB = SystemJob(name="ach-availability", version="1.0.0")
AVAILABLE_AFTER_BANKING_DAYS = 2


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


class HoldMismatch(PaymentsError):
    """A push named a Hold that doesn't reserve its amount on its Customer Balance."""


class PaymentNotYetKnown(PaymentsError):
    """A Provider Event for a Payment the Ledger hasn't recorded yet. Retry later."""


def clearing_account(provider: str, rail: str, currency: str) -> str:
    """The Clearing Account for one Provider, Rail and currency (SPEC §3.1)."""
    return f"clearing:{provider}:{rail}:{currency.lower()}"


def customer_balance_account(customer_id: str, currency: str) -> str:
    """The Customer Balance of one Customer in one currency (SPEC §3.1)."""
    return f"customer:{customer_id}:{currency.lower()}"


_LEDGER_KEY = re.compile(r"^payment:(\d+):(.+)$")


def ledger_key(payment_id: int, step: str) -> str:
    """The Idempotency Key of the Ledger row a Payment's step books, e.g. its pending
    Transaction or its availability Hold."""
    return f"payment:{payment_id}:{step}"


def parse_ledger_key(key: str) -> tuple[int, str] | None:
    """The Payment id and step in a `ledger_key`, or None for any other key."""
    match = _LEDGER_KEY.match(key)
    return None if match is None else (int(match.group(1)), match.group(2))


def recovery_receivable_account(customer_account: str) -> str:
    """The Recovery Receivable for one Customer Balance (one Customer, one currency): what
    that Customer owes Mintwharf after a Return left their balance short (SPEC §3.1)."""
    return f"recovery-receivable:{customer_account}"


def bank_cash_account(currency: str) -> str:
    return f"bank-cash:{currency.lower()}"


def available_at(settled_at: datetime) -> datetime:
    """When ACH money settled at `settled_at` becomes Available (ADR 0004)."""
    day = add_banking_days(new_york_date(settled_at), AVAILABLE_AFTER_BANKING_DAYS)
    return banking_day_starts(day)


@dataclass(frozen=True)
class _Moved:
    """A deficit moved to Recovery Receivable."""

    transaction_id: int
    amount: int
    effective_at: datetime


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
        if new.hold_id is not None:
            self._check_reservation(new)
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
                    hold_id=new.hold_id,
                )
                .on_conflict_do_nothing(index_elements=[payments.c.idempotency_key])
                .returning(payments.c.id)
            )
            if payment_id is not None:
                _append_transition(
                    connection, payment_id, PaymentState.CREATED, now, now, job=ORIGINATION_JOB
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
            raised = _return_exceptions(connection, return_exceptions.c.payment_id == payment_id)
        states = [PaymentState(step.state) for step in steps]
        settled = next((s for s in steps if s.state == PaymentState.SETTLED), None)
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
            hold_id=row.hold_id,
            state=_current(states),
            available_at=None
            if settled is None or row.direction != "pull"
            else available_at(settled.effective_at),
            transitions=[
                Transition(
                    state=PaymentState(step.state),
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
            return_exceptions=raised,
        )

    def owed(self, customer_account: str, connection: Connection | None = None) -> int:
        """What the Customer behind this Customer Balance owes Mintwharf in Recovery
        Receivable now (0 if nothing was ever moved there), read on `connection` if given."""
        journal = self._journal if connection is None else self._journal.within(connection)
        now = self._clock.now()
        try:
            return journal.balances(recovery_receivable_account(customer_account), now, now).posted
        except AccountNotFound:
            return 0

    def return_exceptions(self, customer_account: str | None = None) -> list[ReturnException]:
        """Every return exception raised, oldest first, or only one Customer Balance's."""
        with self._engine.connect() as connection:
            return _return_exceptions(
                connection,
                None
                if customer_account is None
                else return_exceptions.c.customer_account == customer_account,
            )

    def by_keys(
        self, keys: Collection[str], connection: Connection | None = None
    ) -> dict[str, PaymentRef]:
        """The Payments recorded under these Idempotency Keys, by key (a key with no Payment
        is left out), read on `connection` if given."""
        if not keys:
            return {}
        with self._reading(connection) as reading:
            rows = reading.execute(
                select(
                    payments.c.id, payments.c.idempotency_key, payments.c.rail, payments.c.direction
                ).where(payments.c.idempotency_key.in_(list(keys)))
            ).all()
            states: dict[int, list[PaymentState]] = {}
            for payment_id, state in reading.execute(
                select(transitions.c.payment_id, transitions.c.state).where(
                    transitions.c.payment_id.in_([row.id for row in rows])
                )
            ):
                states.setdefault(payment_id, []).append(PaymentState(state))
        return {
            row.idempotency_key: PaymentRef(
                id=row.id,
                idempotency_key=row.idempotency_key,
                rail=row.rail,
                direction=row.direction,
                state=_current(states[row.id]),
            )
            for row in rows
        }

    @contextmanager
    def _reading(self, connection: Connection | None) -> Iterator[Connection]:
        if connection is not None:
            yield connection
        else:
            with self._engine.connect() as fresh:
                yield fresh

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
            reached = _states(connection, payment_id)
            decision = decide(
                _current(reached),
                event.reported,
                reached,
                rail=payment.rail,
                direction=payment.direction,
            )
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
        state: PaymentState,
        inbox_id: int,
        event: ProviderEvent,
    ) -> _Booked:
        actor = ProviderEventActor(inbox_id=str(inbox_id))
        clearing = clearing_account(payment.provider, payment.rail, payment.currency)
        customer = payment.customer_account

        def legs(debit: str, credit: str) -> list[NewEntry]:
            return [
                NewEntry(account=debit, currency=payment.currency, amount=payment.amount),
                NewEntry(account=credit, currency=payment.currency, amount=-payment.amount),
            ]

        kind = f"{payment.rail.upper()} {payment.direction} {payment.provider_payment_id}"
        bank_cash = bank_cash_account(payment.currency)
        pull = payment.direction == "pull"
        # Money in: Clearing Account → Customer Balance, then Clearing Account → Bank Cash.
        # Money out (a Payout): Customer Balance → Clearing Account, then Bank Cash →
        # Clearing Account.
        into_customer = legs(clearing, customer) if pull else legs(customer, clearing)
        into_bank_cash = legs(bank_cash, clearing) if pull else legs(clearing, bank_cash)

        def move(new: NewTransaction) -> _Booked:
            """Post the Transaction that moves the money between the Customer Balance and
            the Clearing Account. A push's reserving Hold, if any, is captured by it."""
            if payment.hold_id is None:
                return _Booked(transaction_id=journal.post(new).transaction.id)
            captured = journal.capture_hold(payment.hold_id, new).transaction
            return _Booked(transaction_id=captured.id, hold_id=payment.hold_id)

        match (payment.rail, state):
            case ("ach", PaymentState.SUBMITTED):
                return move(
                    NewTransaction(
                        idempotency_key=ledger_key(payment.id, "pending"),
                        actor=actor,
                        effective_at=event.occurred_at,
                        description=f"{kind} submitted",
                        status=TransactionStatus.PENDING,
                        entries=into_customer,
                    )
                )
            case ("ach", PaymentState.SETTLED):
                pending_id = _booked_by(connection, payment.id, PaymentState.SUBMITTED)
                assert pending_id is not None
                journal.settle(
                    pending_id,
                    SettlementRequest(
                        idempotency_key=ledger_key(payment.id, "settlement"),
                        actor=actor,
                        effective_at=event.occurred_at,
                    ),
                )
                sweep = self._sweep(journal, payment, actor, event, kind, into_bank_cash)
                if not pull:
                    return _Booked(transaction_id=sweep)
                until = available_at(event.occurred_at)
                hold = journal.place_hold(
                    NewHold(
                        idempotency_key=ledger_key(payment.id, "availability-hold"),
                        actor=actor,
                        effective_at=event.occurred_at,
                        account=customer,
                        currency=payment.currency,
                        amount=payment.amount,
                        description=f"ACH funds Available from {until.isoformat()}",
                    )
                ).hold
                return _Booked(transaction_id=sweep, hold_id=hold.id)
            case ("ach", PaymentState.CANCELED | PaymentState.DECLINED):
                pending_id = _booked_by(connection, payment.id, PaymentState.SUBMITTED)
                if pending_id is None:
                    return _Booked()
                reversal = journal.reverse(
                    pending_id,
                    ReversalRequest(
                        idempotency_key=ledger_key(payment.id, state),
                        actor=actor,
                        effective_at=event.occurred_at,
                        description=f"{kind} {state}",
                    ),
                ).transaction
                return _Booked(transaction_id=reversal.id)
            case ("ach", PaymentState.RETURNED):
                return self._book_return(journal, connection, payment, inbox_id, event, kind)
            case ("rtp", PaymentState.SETTLED):
                paid = move(
                    NewTransaction(
                        idempotency_key=ledger_key(payment.id, "settled"),
                        actor=actor,
                        effective_at=event.occurred_at,
                        description=f"{kind} settled",
                        entries=into_customer,
                    )
                )
                self._sweep(journal, payment, actor, event, kind, into_bank_cash)
                return paid
            case ("rtp", PaymentState.DECLINED):
                return _Booked()  # nothing was booked, so nothing to undo
        raise AssertionError(f"a Provider Event can't book {state}")

    def _book_return(
        self,
        journal: Journal,
        connection: Connection,
        payment: Row[Any],
        inbox_id: int,
        event: ProviderEvent,
        kind: str,
    ) -> _Booked:
        """A Return of a settled pull (ADR 0013): reverse the funding and its sweep to Bank
        Cash, end the availability Hold if it's still in place, then cover any deficit the
        Customer Balance is left with, from the Return's Effective Time on, by moving it to
        Recovery Receivable, raising a return exception for each move."""
        code = event.return_code
        assert code is not None  # the Provider adapter refuses a Return without its code
        actor = ProviderEventActor(inbox_id=str(inbox_id))
        described = f"{kind} returned {code.code}"
        # Returns on one Customer Balance are booked one at a time, so each measures the
        # deficit with the other's Reversal in it.
        lock_name(connection, LockSpace.CUSTOMER, payment.customer_account)
        settled = connection.execute(
            select(
                transitions.c.transaction_id, transitions.c.hold_id, transitions.c.effective_at
            ).where(
                transitions.c.payment_id == payment.id,
                transitions.c.state == PaymentState.SETTLED,
            )
        ).one()
        funding = _booked_by(connection, payment.id, PaymentState.SUBMITTED)
        assert funding is not None
        reversal = self._reverse(journal, payment, funding, "returned", described, actor, event)
        self._reverse(
            journal,
            payment,
            settled.transaction_id,
            "bank-cash-returned",
            f"{described}, out of Bank Cash",
            actor,
            event,
        )
        self._end_availability_hold(journal, payment, settled.hold_id, actor, event)
        late = is_late(settled.effective_at, event.occurred_at)
        for moved in self._cover_deficit(journal, payment, actor, event, described):
            _record_return_exception(
                connection, payment, inbox_id, code, late, moved, self._clock.now()
            )
        return _Booked(transaction_id=reversal)

    @staticmethod
    def _reverse(
        journal: Journal,
        payment: Row[Any],
        transaction_id: int,
        step: str,
        description: str,
        actor: ProviderEventActor,
        event: ProviderEvent,
    ) -> int:
        return journal.reverse(
            transaction_id,
            ReversalRequest(
                idempotency_key=ledger_key(payment.id, step),
                actor=actor,
                effective_at=event.occurred_at,
                description=description,
            ),
        ).transaction.id

    @staticmethod
    def _end_availability_hold(
        journal: Journal,
        payment: Row[Any],
        hold_id: int | None,
        actor: ProviderEventActor,
        event: ProviderEvent,
    ) -> None:
        """Release the availability Hold if the funds haven't been made Available yet, so
        returned money never is."""
        if hold_id is None or journal.hold(hold_id).ended is not None:
            return
        journal.release_hold(
            hold_id,
            ReleaseRequest(
                idempotency_key=ledger_key(payment.id, "returned-hold"),
                actor=actor,
                effective_at=event.occurred_at,
            ),
        )

    def _cover_deficit(
        self,
        journal: Journal,
        payment: Row[Any],
        actor: ProviderEventActor,
        event: ProviderEvent,
        described: str,
    ) -> list["_Moved"]:
        """Walk the Customer Balance from the Return's Effective Time through every later
        moment it changes, as known now, and wherever its Posted Balance less unsettled
        outflows is below zero, move the shortfall to Recovery Receivable at that moment.
        Each move only tops up: a later inflow never takes one back (collecting is for
        Reconciliation, #9)."""
        customer = payment.customer_account
        now = self._clock.now()
        moments = [event.occurred_at, *journal.balance_moments(customer, event.occurred_at, now)]
        moved: list[_Moved] = []
        for moment in moments:
            standing = journal.balances(customer, moment, now)
            short = standing.unsettled_outflows - standing.posted
            if short <= 0:
                continue
            recovery = recovery_receivable_account(customer)
            _ensure_asset_account(journal, recovery, payment.currency)
            step = "recovery" if not moved else f"recovery-{len(moved) + 1}"
            transaction = journal.post(
                NewTransaction(
                    idempotency_key=ledger_key(payment.id, step),
                    actor=actor,
                    effective_at=moment,
                    description=f"{described}: deficit to Recovery Receivable",
                    entries=[
                        NewEntry(account=recovery, currency=payment.currency, amount=short),
                        NewEntry(account=customer, currency=payment.currency, amount=-short),
                    ],
                )
            ).transaction.id
            moved.append(_Moved(transaction, short, moment))
        return moved

    @staticmethod
    def _sweep(
        journal: Journal,
        payment: Row[Any],
        actor: ProviderEventActor,
        event: ProviderEvent,
        kind: str,
        entries: list[NewEntry],
    ) -> int:
        """The posted Transaction between Bank Cash and the Clearing Account at settlement,
        which brings the Clearing Account back to zero."""
        return journal.post(
            NewTransaction(
                idempotency_key=ledger_key(payment.id, "bank-cash"),
                actor=actor,
                effective_at=event.occurred_at,
                description=f"{kind} settled to Bank Cash",
                entries=entries,
            )
        ).transaction.id

    # Availability

    def make_available(self) -> int:
        """Release the availability Hold of every settled ACH pull whose time has come,
        at its `available_at`, and record it `available`. Returns how many it moved."""
        now = self._clock.now()
        later = transitions.alias("later")
        with self._engine.connect() as connection:
            settled = connection.execute(
                select(transitions.c.payment_id, transitions.c.effective_at)
                .join(payments, payments.c.id == transitions.c.payment_id)
                .where(
                    payments.c.direction == "pull",
                    transitions.c.state == PaymentState.SETTLED,
                    ~select(later.c.id)
                    .where(
                        later.c.payment_id == transitions.c.payment_id,
                        later.c.state.in_([PaymentState.AVAILABLE, PaymentState.RETURNED]),
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
            if _current(_states(connection, payment_id)) is not PaymentState.SETTLED:
                return False
            hold_id: int = connection.execute(
                select(transitions.c.hold_id).where(
                    transitions.c.payment_id == payment_id,
                    transitions.c.state == PaymentState.SETTLED,
                )
            ).scalar_one()
            self._journal.within(connection).release_hold(
                hold_id,
                ReleaseRequest(
                    idempotency_key=ledger_key(payment_id, "available"),
                    actor=AVAILABILITY_JOB,
                    effective_at=due,
                ),
            )
            _append_transition(
                connection,
                payment_id,
                PaymentState.AVAILABLE,
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

    def _check_reservation(self, new: NewPayment) -> None:
        assert new.hold_id is not None
        try:
            hold = self._journal.hold(new.hold_id)
        except HoldNotFound as error:
            raise HoldMismatch(f"no Hold {new.hold_id}") from error
        if (hold.account, hold.currency, hold.amount) != (
            new.customer_account,
            new.currency,
            new.amount,
        ):
            raise HoldMismatch(
                f"Hold {new.hold_id} doesn't reserve {new.amount} on {new.customer_account}"
            )

    def _ensure_account(self, code: str, new: NewPayment) -> None:
        try:
            self._journal.account(code)
        except AccountNotFound:
            with suppress(DuplicateAccount):  # opened concurrently
                self._journal.open_account(
                    NewAccount(code=code, type=AccountType.ASSET, currency=new.currency)
                )


def _current(states: list[PaymentState]) -> PaymentState:
    ended = [state for state in states if state in ENDS_BEFORE_SETTLEMENT]
    if ended:
        return ended[0]
    if PaymentState.RETURNED in states:
        return PaymentState.RETURNED
    return max(states, key=MAIN_PATH.index)


def _states(connection: Connection, payment_id: int) -> list[PaymentState]:
    return [
        PaymentState(state)
        for state in connection.scalars(
            select(transitions.c.state).where(transitions.c.payment_id == payment_id)
        )
    ]


def _booked_by(connection: Connection, payment_id: int, state: PaymentState) -> int | None:
    found: int | None = connection.scalar(
        select(transitions.c.transaction_id).where(
            transitions.c.payment_id == payment_id, transitions.c.state == state
        )
    )
    return found


def _ensure_asset_account(journal: Journal, code: str, currency: str) -> None:
    try:
        journal.account(code)
    except AccountNotFound:
        # Safe inside the caller's transaction: the Journal opens it in a savepoint, so a
        # concurrent opening only rolls that back.
        with suppress(DuplicateAccount):
            journal.open_account(NewAccount(code=code, type=AccountType.ASSET, currency=currency))


def _record_return_exception(
    connection: Connection,
    payment: Row[Any],
    inbox_id: int,
    code: ReturnCode,
    late: bool,
    moved: _Moved,
    recorded_at: datetime,
) -> None:
    detail = (
        f"{'Late ' if late else ''}Return {code.code} of Payment {payment.id} left "
        f"{payment.customer_account} short by {moved.amount} {payment.currency} Minor Units at "
        f"{moved.effective_at.isoformat()}, moved to Recovery Receivable"
    )
    connection.execute(
        insert(return_exceptions).values(
            payment_id=payment.id,
            inbox_id=inbox_id,
            customer_account=payment.customer_account,
            recovery_account=recovery_receivable_account(payment.customer_account),
            currency=payment.currency,
            return_code=code.code,
            exposure=moved.amount,
            late=late,
            recovery_transaction_id=moved.transaction_id,
            detail=detail,
            effective_at=moved.effective_at,
            recorded_at=recorded_at,
        )
    )
    log.warning(
        "Return left a Customer Balance short",
        extra={"payment_id": payment.id, "exposure": moved.amount, "return_code": code.code},
    )


def _return_exceptions(connection: Connection, where: Any) -> list[ReturnException]:
    query = select(return_exceptions).order_by(return_exceptions.c.id)
    if where is not None:
        query = query.where(where)
    return [
        ReturnException.model_validate({k: v for k, v in row._asdict().items() if k != "id"})
        for row in connection.execute(query)
    ]


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
    state: PaymentState,
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
    lock(connection, LockSpace.PAYMENT, payment_id)
