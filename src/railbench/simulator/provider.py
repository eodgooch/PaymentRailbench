"""The Simulator playing a Provider: Payments, their timing on the Banking Day calendar, and
signed webhook delivery with retries (SPEC §6, ADR 0010).

An ACH pull is submitted when it's created. It counts toward the Banking Day it was
submitted on if that's a Banking Day and it's before the ACH Cutoff (16:00 New York time),
otherwise toward the next Banking Day, and it settles at 08:30 New York time on the Banking
Day after that. Each change emits a Provider Event (`payment.submitted`,
`payment.settled`) whose body is frozen when it's emitted, so every delivery attempt sends
the same event ID and bytes, signed afresh.

Nothing happens on its own: `tick()` settles what's due by the Clock and delivers due
events. A delivery that doesn't get a 2xx (or doesn't connect, or times out) is retried
after 30 s, then 60 s, doubling to at most an hour, and abandoned after MAX_ATTEMPTS.
Events go out in the order they were emitted, claimed briefly and POSTed outside any
database transaction, so a slow receiver holds no locks.

An ACH push (a Payout) runs on the same timetable as a pull. An RTP push (RTP is credit push
only) is instant and final: it settles, or is declined if so planned, the moment it's
created, on any day at any hour (RTP runs 24/7/365, so neither the Cutoff nor the Banking
Day calendar applies), and emits that one Provider Event (`payment.settled` or
`payment.declined`). Nothing happens to it afterwards: RTP has no Returns.

A Scenario scripts a Payment before creating it (`plan()`, ADR 0011): how it ends (settles,
or is declined or canceled at the time it would have settled) and which delivery Faults to
inject. The Faults act on the outbox when the event is emitted: a duplicate adds more
delivery rows with the same event ID and body; a delay pushes the first attempt out; an
out-of-order Fault holds the event until a later event of the same Payment has been
delivered. Each injected Fault is written to the Fault Manifest (`faults()`) with its true
root cause. Payment IDs are derived from the Payment's Idempotency Key, and event IDs from
that key and the event type, so a Scenario run names its Payments and events the same way
every time, whatever else the database holds.

A settled ACH pull can be returned with a NACHA return code (`payment.returned`, its body
carrying the code and reason), either as planned (a `return` Fault, `at` after it settles)
or injected by hand (`return_payment()`, now). Either way it must land inside the code's
Return Window (`railbench.common.returns`), measured from the settlement: a planned Return
is checked when the Payment is created, since that's when its settlement time is known, and
the Payment is refused (ReturnOutsideWindow) if it would fall outside. Only an ACH pull
can be returned: RTP has no Returns, and a Payout's Return isn't simulated. A Return is a
Fault in the manifest: cause `late_return` once the 2-Banking-Day window (and so the
Ledger's availability) has passed, `ach_return` inside it. Delivery Faults act on the
`payment.returned` event like any other.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import Connection, Engine, Row, and_, exists, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from railbench.common.calendar import at_new_york, is_banking_day, new_york_date, next_banking_day
from railbench.common.clock import Clock
from railbench.common.fingerprint import fingerprint as payload_fingerprint
from railbench.common.returns import RETURN_CODES, WindowKind, is_late, return_deadline
from railbench.common.signing import SIGNATURE_HEADER, sign
from railbench.simulator.model import (
    FINAL_EVENT,
    STATUS_FOR_OUTCOME,
    DelayedDelivery,
    DuplicateDelivery,
    EventName,
    EventType,
    FaultPlan,
    InjectedFault,
    OutOfOrderDelivery,
    PaymentPlan,
    PaymentRequest,
    PaymentStatus,
    PlannedReturn,
    SimulatedPayment,
)
from railbench.simulator.tables import events, faults, payments, plans

ACH_CUTOFF = time(16, 0)
ACH_SETTLEMENT_TIME = time(8, 30)
FIRST_RETRY = timedelta(seconds=30)
LONGEST_RETRY = timedelta(hours=1)
MAX_ATTEMPTS = 10
CLAIM_LEASE = timedelta(minutes=1)
DELIVERY_TIMEOUT = 5.0


class PaymentNotFound(LookupError):
    pass


class IdempotencyConflict(Exception):
    """The Idempotency Key was already used with a different payload."""


class DeliveryFailed(RuntimeError):
    """A webhook delivery failed while the Simulator was told every delivery must succeed
    (a Scenario run, where a retry would make the run diverge from its seed)."""


class PlanConflict(Exception):
    """A different plan was already made for this Idempotency Key."""


class PlanMismatch(Exception):
    """A Payment created on another Rail than the one it was planned for, or one that can't
    do what was planned for it (a Return for anything but an ACH pull)."""


class ReturnRefused(Exception):
    """The Payment can't be returned now: it isn't a settled ACH pull, or it was returned
    already."""


class ReturnOutsideWindow(ValueError):
    """A Return outside its code's Return Window, or with a code the Simulator doesn't know."""


@dataclass(frozen=True)
class Delivery:
    event_id: str
    type: str
    attempts: int
    delivered: bool
    abandoned: bool
    last_error: str


def ach_settles_at(submitted_at: datetime) -> datetime:
    """When an ACH Payment submitted at `submitted_at` settles."""
    day = new_york_date(submitted_at)
    if not is_banking_day(day) or at_new_york(day, ACH_CUTOFF) <= submitted_at:
        day = next_banking_day(day)
    return at_new_york(next_banking_day(day), ACH_SETTLEMENT_TIME)


def payment_id_for(idempotency_key: str) -> str:
    """The Simulator's ID for the Payment created under `idempotency_key`, derived from the
    key rather than a database sequence, so a Scenario run names its Payments the same way
    whatever else the database holds."""
    return f"simpay_{hashlib.sha256(f'payment\x00{idempotency_key}'.encode()).hexdigest()[:24]}"


def event_id_for(idempotency_key: str, type_: EventType) -> str:
    """The Provider event ID of a Payment's event of this type: one per (Payment, type), the
    same in every run of a Scenario, whatever the database's sequences say."""
    digest = hashlib.sha256(f"{idempotency_key}\x00{type_}".encode()).hexdigest()
    return f"simevt_{digest[:24]}"


def _event_type(name: EventName) -> EventType:
    return EventType(f"payment.{name}")


def retry_delay(attempts: int) -> timedelta:
    """How long to wait after the `attempts`th failed delivery."""
    return min(FIRST_RETRY * (1 << (attempts - 1)), LONGEST_RETRY)


class Simulator:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        webhook_url: str,
        signing_secret: str,
        http: httpx.Client | None = None,
        fail_on_delivery_error: bool = False,
    ) -> None:
        self._engine = engine
        self._fail_on_delivery_error = fail_on_delivery_error
        self._clock = clock
        self._webhook_url = webhook_url
        self._secret = signing_secret
        self._http = http or httpx.Client(timeout=DELIVERY_TIMEOUT)

    def create_payment(self, request: PaymentRequest) -> tuple[SimulatedPayment, bool]:
        """Create a Payment once per Idempotency Key: submit an ACH one, settle (or decline)
        an RTP one at once. Returns the Payment and whether this was a replay of an earlier
        request."""
        fingerprint = payload_fingerprint(request.model_dump(exclude={"idempotency_key"}))
        now = self._clock.now()
        with self._engine.begin() as connection:
            plan = _stored_plan(connection, request.idempotency_key)
            if plan is not None and plan.rail != request.rail:
                raise PlanMismatch(
                    f"{request.idempotency_key!r} was planned as {plan.rail}, not {request.rail}"
                )
            planned_return = None if plan is None else plan.planned_return
            if planned_return is not None and request.direction != "pull":
                raise PlanMismatch(
                    f"{request.idempotency_key!r} plans a Return: only an ACH pull is returned"
                )
            outcome = "settle" if plan is None else plan.outcome
            if request.rail == "rtp":
                status = STATUS_FOR_OUTCOME[outcome]
                timing: dict[str, Any] = (
                    {"settled_at": now} if outcome == "settle" else {"ended_at": now}
                )
                first_event = _event_type(FINAL_EVENT[outcome])
            else:
                status = PaymentStatus.SUBMITTED
                settles_at = ach_settles_at(now)
                timing = {"submitted_at": now, "settles_at": settles_at}
                if planned_return is not None:
                    timing |= _planned_return(planned_return, settles_at)
                first_event = EventType.SUBMITTED
            payment_id = connection.scalar(
                pg_insert(payments)
                .values(
                    public_id=payment_id_for(request.idempotency_key),
                    idempotency_key=request.idempotency_key,
                    payload_fingerprint=fingerprint,
                    rail=request.rail,
                    direction=request.direction,
                    currency=request.currency,
                    amount=request.amount,
                    reference=request.reference,
                    status=status,
                    outcome=outcome,
                    created_at=now,
                    **timing,
                )
                .on_conflict_do_nothing(index_elements=[payments.c.idempotency_key])
                .returning(payments.c.id)
            )
            if payment_id is not None:
                self._emit(connection, payment_id, first_event, now)
        with self._engine.connect() as connection:
            row = connection.execute(
                select(payments).where(payments.c.idempotency_key == request.idempotency_key)
            ).one()
        if row.payload_fingerprint != fingerprint:
            raise IdempotencyConflict(
                f"Idempotency Key {request.idempotency_key!r} was used with another payload"
            )
        return _payment(row), payment_id is None

    def plan(self, plan: PaymentPlan) -> None:
        """Script the Payment that will be created under `plan.idempotency_key`. Planning
        the same thing twice is fine; a different plan for the same key is a PlanConflict.
        A Payment created without a plan settles, with no Faults."""
        faults_json = [fault.model_dump(mode="json") for fault in plan.faults]
        with self._engine.begin() as connection:
            connection.execute(
                pg_insert(plans)
                .values(
                    idempotency_key=plan.idempotency_key,
                    rail=plan.rail,
                    outcome=plan.outcome,
                    faults=faults_json,
                )
                .on_conflict_do_nothing(index_elements=[plans.c.idempotency_key])
            )
            stored = connection.execute(
                select(plans.c.rail, plans.c.outcome, plans.c.faults).where(
                    plans.c.idempotency_key == plan.idempotency_key
                )
            ).one()
        if (stored.rail, stored.outcome, stored.faults) != (plan.rail, plan.outcome, faults_json):
            raise PlanConflict(f"{plan.idempotency_key!r} already has a different plan")

    def faults(self) -> list[InjectedFault]:
        """The Fault Manifest: every Fault injected so far, in the order it was injected."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(faults, payments.c.idempotency_key, payments.c.public_id)
                .join(payments, payments.c.id == faults.c.payment_id)
                .order_by(faults.c.id)
            ).all()
        return [
            InjectedFault(
                fault_id=row.fault_id,
                kind=row.kind,
                payment_id=row.public_id,
                idempotency_key=row.idempotency_key,
                event_id=row.event_id,
                event_type=row.event_type,
                params=row.params,
                injected_at=row.injected_at,
                cause_code=row.cause_code,
                root_cause=row.root_cause,
            )
            for row in rows
        ]

    def next_due(self) -> datetime | None:
        """When `tick()` next has something to do: a Payment to settle or end, or a webhook
        to deliver. None when nothing is waiting. An event held for out-of-order delivery
        isn't due until the event it waits for has gone."""
        with self._engine.connect() as connection:
            settling = connection.scalar(
                select(func.min(payments.c.settles_at)).where(
                    payments.c.status == PaymentStatus.SUBMITTED
                )
            )
            delivering = connection.scalar(
                select(func.min(events.c.next_attempt_at)).where(_deliverable())
            )
            returning = connection.scalar(
                select(func.min(payments.c.returns_at)).where(
                    payments.c.status == PaymentStatus.SETTLED
                )
            )
        due = [moment for moment in (settling, delivering, returning) if moment is not None]
        return min(due) if due else None

    def payment(self, public_id: str) -> SimulatedPayment:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(payments).where(payments.c.public_id == public_id)
            ).one_or_none()
        if row is None:
            raise PaymentNotFound(public_id)
        return _payment(row)

    def deliveries(self, public_id: str) -> list[Delivery]:
        """The Provider Events emitted for a Payment and how their delivery stands."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(events)
                .join(payments, payments.c.id == events.c.payment_id)
                .where(payments.c.public_id == public_id)
                .order_by(events.c.id)
            ).all()
        return [
            Delivery(
                event_id=row.event_id,
                type=row.type,
                attempts=row.attempts,
                delivered=row.delivered_at is not None,
                abandoned=row.abandoned_at is not None,
                last_error=row.last_error,
            )
            for row in rows
        ]

    def return_payment(self, public_id: str, code: str) -> SimulatedPayment:
        """Return a settled ACH pull now, with NACHA return `code`: a Return injected by hand
        rather than planned. Raises ReturnRefused for anything but a settled ACH pull, and
        ReturnOutsideWindow outside the code's Return Window."""
        if code not in RETURN_CODES:
            raise ReturnOutsideWindow(f"the Simulator knows no NACHA return code {code!r}")
        now = self._clock.now()
        with self._engine.begin() as connection:
            row = connection.execute(
                select(payments).where(payments.c.public_id == public_id).with_for_update()
            ).one_or_none()
            if row is None:
                raise PaymentNotFound(public_id)
            if row.rail != "ach":
                raise ReturnRefused(f"{public_id} is an RTP Payment: RTP has no Returns")
            if row.direction != "pull":
                raise ReturnRefused(f"{public_id} is a push: only an ACH pull is returned")
            if row.status != PaymentStatus.SETTLED:
                raise ReturnRefused(f"{public_id} is {row.status}: only a settled pull returns")
            _check_window(code, row.settled_at, now)
            self._return(connection, row.id, code, now, injected=True)
        return self.payment(public_id)

    def tick(self) -> None:
        """Do what's due by the Clock: settle ACH Payments, return the ones planned to be,
        then deliver webhooks."""
        self._settle_due()
        self._return_due()
        self._deliver_due()

    def _return_due(self) -> None:
        now = self._clock.now()
        with self._engine.begin() as connection:
            due = connection.execute(
                select(payments.c.id, payments.c.return_code, payments.c.returns_at)
                .where(payments.c.status == PaymentStatus.SETTLED, payments.c.returns_at <= now)
                .order_by(payments.c.returns_at, payments.c.id)
                .with_for_update(skip_locked=True)
            ).all()
            for payment_id, code, returns_at in due:
                self._return(connection, payment_id, code, returns_at, injected=False)

    def _return(
        self,
        connection: Connection,
        payment_id: int,
        code: str,
        returned_at: datetime,
        *,
        injected: bool,
    ) -> None:
        connection.execute(
            update(payments)
            .where(payments.c.id == payment_id)
            .values(
                status=PaymentStatus.RETURNED,
                return_code=code,
                returns_at=returned_at,
                returned_at=returned_at,
            )
        )
        injection = (
            PlannedReturn(id="injected-return", code=code, at=timedelta(0)) if injected else None
        )
        self._emit(connection, payment_id, EventType.RETURNED, returned_at, injection)

    def _settle_due(self) -> None:
        """Settle, or decline or cancel as planned, every submitted Payment that's due: the
        planned ending happens at the time the Payment would have settled."""
        now = self._clock.now()
        with self._engine.begin() as connection:
            due = connection.execute(
                select(payments.c.id, payments.c.settles_at, payments.c.outcome)
                .where(payments.c.status == PaymentStatus.SUBMITTED, payments.c.settles_at <= now)
                .order_by(payments.c.id)
                .with_for_update(skip_locked=True)
            ).all()
            for payment_id, settles_at, outcome in due:
                status = STATUS_FOR_OUTCOME[outcome]
                ended = (
                    {"settled_at": settles_at} if outcome == "settle" else {"ended_at": settles_at}
                )
                connection.execute(
                    update(payments)
                    .where(payments.c.id == payment_id)
                    .values(status=status, **ended)
                )
                self._emit(connection, payment_id, _event_type(FINAL_EVENT[outcome]), settles_at)

    def _emit(
        self,
        connection: Connection,
        payment_id: int,
        type_: EventType,
        occurred_at: datetime,
        injected_return: PlannedReturn | None = None,
    ) -> None:
        """Freeze a Provider Event into the outbox, with any Faults planned for it (and the
        Return injected by hand, if this is one)."""
        row = connection.execute(select(payments).where(payments.c.id == payment_id)).one()
        now = self._clock.now()
        event_id = event_id_for(row.idempotency_key, type_)
        event: dict[str, Any] = {
            "event_id": event_id,
            "type": type_,
            "occurred_at": occurred_at,
            "payment": _payment(row).model_dump(mode="json"),
        }
        if type_ is EventType.RETURNED:
            event["return"] = {
                "code": row.return_code,
                "reason": RETURN_CODES[row.return_code].reason,
            }
        body = json.dumps(
            event,
            default=_iso,
            separators=(",", ":"),
        )
        delivery: dict[str, Any] = {
            "payment_id": payment_id,
            "event_id": event_id,
            "type": type_,
            "occurred_at": occurred_at,
            "body": body,
            "next_attempt_at": now,
        }
        copies: list[DuplicateDelivery] = []
        injected: list[tuple[FaultPlan, str, str]] = []
        planned = self._faults_for(connection, row.idempotency_key, type_)
        if injected_return is not None:  # injected by hand instead of as planned
            planned = [f for f in planned if not isinstance(f, PlannedReturn)]
        for fault in [*planned, *([injected_return] if injected_return else [])]:
            match fault:
                case PlannedReturn(code=code):
                    returned = RETURN_CODES[code]
                    late = is_late(row.settled_at, occurred_at)
                    window = (
                        "60 calendar days"
                        if returned.window is WindowKind.EXTENDED
                        else "2 Banking Days"
                    )
                    cause = (
                        f"The Simulator returned ACH pull {row.public_id} with {code} "
                        f"({returned.reason}) in Provider Event {event_id} at "
                        f"{_iso(occurred_at)}, inside its {window} Return Window (until "
                        f"{_iso(return_deadline(code, row.settled_at))}), "
                        + (
                            "after the funds had become Available (a Late Return)."
                            if late
                            else "before the funds became Available."
                        )
                    )
                    injected.append((fault, "late_return" if late else "ach_return", cause))
                case DelayedDelivery(by=by):
                    delivery["next_attempt_at"] += by
                    cause = (
                        f"The Simulator held Provider Event {event_id} ({type_}) back for {by} "
                        "past when it was due (a delayed webhook)."
                    )
                    injected.append((fault, "delayed_delivery", cause))
                case OutOfOrderDelivery(after=after):
                    later = _event_type(after)
                    delivery["after_event_id"] = event_id_for(row.idempotency_key, later)
                    cause = (
                        f"The Simulator held Provider Event {event_id} ({type_}) until "
                        f"{delivery['after_event_id']} ({later}) had been delivered, so the "
                        "Ledger heard of the later state first (out-of-order webhooks)."
                    )
                    injected.append((fault, "out_of_order_delivery", cause))
                case DuplicateDelivery(times=times):
                    copies.append(fault)
                    cause = (
                        f"The Simulator delivered Provider Event {event_id} ({type_}) "
                        f"{times + 1} times with the same event ID and body (a duplicate "
                        "webhook)."
                    )
                    injected.append((fault, "duplicate_delivery", cause))
        held_by = next(
            (f.id for f, _, _ in injected if isinstance(f, DelayedDelivery | OutOfOrderDelivery)),
            None,
        )
        connection.execute(insert(events).values(**delivery, fault_id=held_by))
        for copy in copies:
            extra = {**delivery, "fault_id": copy.id}
            if copy.after is not None:
                extra["after_event_id"] = event_id_for(row.idempotency_key, _event_type(copy.after))
            connection.execute(insert(events), [extra] * copy.times)
        for fault, cause_code, root_cause in injected:
            connection.execute(
                insert(faults).values(
                    fault_id=fault.id,
                    kind=fault.kind,
                    payment_id=payment_id,
                    event_id=event_id,
                    event_type=type_,
                    params=fault.model_dump(
                        mode="json", exclude={"id", "kind", "event"}, exclude_none=True
                    ),
                    injected_at=now,
                    cause_code=cause_code,
                    root_cause=root_cause,
                )
            )

    @staticmethod
    def _faults_for(
        connection: Connection, idempotency_key: str, type_: EventType
    ) -> list[FaultPlan]:
        plan = _stored_plan(connection, idempotency_key)
        if plan is None:
            return []
        return [f for f in plan.faults if _event_type(f.event) == type_]

    def _deliver_due(self) -> None:
        """Claim the due events in one short commit, POST each outside any transaction, and
        record each result in its own short commit, so a slow receiver holds no locks.
        Claiming pushes `next_attempt_at` out by CLAIM_LEASE: a concurrent tick skips the
        claimed events, and an event whose result never got recorded (a crash mid-POST) is
        retried once the lease runs out."""
        now = self._clock.now()
        with self._engine.begin() as connection:
            claimed = connection.execute(
                select(events.c.id, events.c.body, events.c.attempts)
                .where(_deliverable(), events.c.next_attempt_at <= now)
                .order_by(events.c.id)
                .with_for_update(skip_locked=True)
            ).all()
            if claimed:
                connection.execute(
                    update(events)
                    .where(events.c.id.in_([row.id for row in claimed]))
                    .values(next_attempt_at=now + CLAIM_LEASE)
                )
        for event_id, body, attempts in claimed:
            error = self._post(body.encode())
            self._record_attempt(event_id, attempts + 1, error)
            if error is not None and self._fail_on_delivery_error:
                raise DeliveryFailed(f"delivering outbox row {event_id}: {error}")

    def _record_attempt(self, event_id: int, attempts: int, error: str | None) -> None:
        now = self._clock.now()
        if error is None:
            values: dict[str, Any] = {"attempts": attempts, "delivered_at": now}
        elif attempts >= MAX_ATTEMPTS:
            values = {"attempts": attempts, "abandoned_at": now, "last_error": error}
        else:
            values = {
                "attempts": attempts,
                "next_attempt_at": now + retry_delay(attempts),
                "last_error": error,
            }
        with self._engine.begin() as connection:
            connection.execute(update(events).where(events.c.id == event_id).values(values))

    def _post(self, body: bytes) -> str | None:
        """Deliver one webhook. None on a 2xx, otherwise what went wrong."""
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(self._secret, body, self._clock.now()),
        }
        try:
            response = self._http.post(self._webhook_url, content=body, headers=headers)
        except httpx.HTTPError as error:
            return f"{type(error).__name__}: {error}"
        if response.is_success:
            return None
        return f"HTTP {response.status_code}"


def _stored_plan(connection: Connection, idempotency_key: str) -> PaymentPlan | None:
    stored = connection.execute(
        select(plans.c.rail, plans.c.outcome, plans.c.faults).where(
            plans.c.idempotency_key == idempotency_key
        )
    ).one_or_none()
    if stored is None:
        return None
    return PaymentPlan.model_validate(
        {
            "idempotency_key": idempotency_key,
            "rail": stored.rail,
            "outcome": stored.outcome,
            "faults": stored.faults,
        }
    )


def _planned_return(planned: PlannedReturn, settles_at: datetime) -> dict[str, Any]:
    """The columns scheduling a planned Return, once the settlement time is known."""
    returns_at = settles_at + planned.at
    _check_window(planned.code, settles_at, returns_at)
    return {"return_code": planned.code, "returns_at": returns_at}


def _check_window(code: str, settled_at: datetime, returned_at: datetime) -> None:
    deadline = return_deadline(code, settled_at)
    if not settled_at <= returned_at <= deadline:
        raise ReturnOutsideWindow(
            f"a {code} Return at {_iso(returned_at)} is outside its Return Window: from "
            f"{_iso(settled_at)} to {_iso(deadline)}"
        )


def _deliverable() -> Any:
    """An outbox row still to deliver, and not held back for an event yet to go."""
    sent = events.alias("sent")
    return and_(
        events.c.delivered_at.is_(None),
        events.c.abandoned_at.is_(None),
        or_(
            events.c.after_event_id.is_(None),
            exists().where(
                sent.c.event_id == events.c.after_event_id,
                or_(sent.c.delivered_at.is_not(None), sent.c.abandoned_at.is_not(None)),
            ),
        ),
    )


def _payment(row: Row[Any]) -> SimulatedPayment:
    return SimulatedPayment(
        id=row.public_id,
        idempotency_key=row.idempotency_key,
        rail=row.rail,
        direction=row.direction,
        currency=row.currency,
        amount=row.amount,
        reference=row.reference,
        status=PaymentStatus(row.status),
        created_at=row.created_at,
        submitted_at=row.submitted_at,
        settled_at=row.settled_at,
        ended_at=row.ended_at,
        returned_at=row.returned_at,
        return_code=row.return_code if row.returned_at is not None else None,
    )


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    raise TypeError(f"can't serialise {type(value).__name__}")
