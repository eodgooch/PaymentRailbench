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
database transaction, so a slow receiver holds no locks. Faults (duplicates, reordering, delays) and
Returns join in later issues.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import Connection, Engine, Row, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from railbench.common.calendar import at_new_york, is_banking_day, new_york_date, next_banking_day
from railbench.common.clock import Clock
from railbench.common.fingerprint import fingerprint as payload_fingerprint
from railbench.common.signing import SIGNATURE_HEADER, sign
from railbench.simulator.model import EventType, PaymentRequest, PaymentStatus, SimulatedPayment
from railbench.simulator.tables import events, payments

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
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._webhook_url = webhook_url
        self._secret = signing_secret
        self._http = http or httpx.Client(timeout=DELIVERY_TIMEOUT)

    def create_payment(self, request: PaymentRequest) -> tuple[SimulatedPayment, bool]:
        """Create and submit a Payment once per Idempotency Key. Returns the Payment and
        whether this was a replay of an earlier request."""
        fingerprint = payload_fingerprint(request.model_dump(exclude={"idempotency_key"}))
        now = self._clock.now()
        with self._engine.begin() as connection:
            payment_id = connection.scalar(
                pg_insert(payments)
                .values(
                    idempotency_key=request.idempotency_key,
                    payload_fingerprint=fingerprint,
                    rail=request.rail,
                    direction=request.direction,
                    currency=request.currency,
                    amount=request.amount,
                    reference=request.reference,
                    status=PaymentStatus.SUBMITTED,
                    created_at=now,
                    submitted_at=now,
                    settles_at=ach_settles_at(now),
                )
                .on_conflict_do_nothing(index_elements=[payments.c.idempotency_key])
                .returning(payments.c.id)
            )
            if payment_id is not None:
                self._emit(connection, payment_id, EventType.SUBMITTED, now)
        with self._engine.connect() as connection:
            row = connection.execute(
                select(payments).where(payments.c.idempotency_key == request.idempotency_key)
            ).one()
        if row.payload_fingerprint != fingerprint:
            raise IdempotencyConflict(
                f"Idempotency Key {request.idempotency_key!r} was used with another payload"
            )
        return _payment(row), payment_id is None

    def payment(self, public_id: str) -> SimulatedPayment:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(payments).where(payments.c.id == _internal_id(public_id))
            ).one_or_none()
        if row is None:
            raise PaymentNotFound(public_id)
        return _payment(row)

    def deliveries(self, public_id: str) -> list[Delivery]:
        """The Provider Events emitted for a Payment and how their delivery stands."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(events)
                .where(events.c.payment_id == _internal_id(public_id))
                .order_by(events.c.id)
            ).all()
        return [
            Delivery(
                event_id=_event_id(row.id),
                type=row.type,
                attempts=row.attempts,
                delivered=row.delivered_at is not None,
                abandoned=row.abandoned_at is not None,
                last_error=row.last_error,
            )
            for row in rows
        ]

    def tick(self) -> None:
        """Do what's due by the Clock: settle ACH Payments, then deliver webhooks."""
        self._settle_due()
        self._deliver_due()

    def _settle_due(self) -> None:
        now = self._clock.now()
        with self._engine.begin() as connection:
            due = connection.execute(
                select(payments.c.id, payments.c.settles_at)
                .where(payments.c.status == PaymentStatus.SUBMITTED, payments.c.settles_at <= now)
                .order_by(payments.c.id)
                .with_for_update(skip_locked=True)
            ).all()
            for payment_id, settles_at in due:
                connection.execute(
                    update(payments)
                    .where(payments.c.id == payment_id)
                    .values(status=PaymentStatus.SETTLED, settled_at=settles_at)
                )
                self._emit(connection, payment_id, EventType.SETTLED, settles_at)

    def _emit(
        self, connection: Connection, payment_id: int, type_: EventType, occurred_at: datetime
    ) -> None:
        row = connection.execute(select(payments).where(payments.c.id == payment_id)).one()
        event_id = connection.scalar(
            insert(events)
            .values(
                payment_id=payment_id,
                type=type_,
                occurred_at=occurred_at,
                body="",
                next_attempt_at=self._clock.now(),
            )
            .returning(events.c.id)
        )
        body = {
            "event_id": _event_id(event_id),
            "type": type_,
            "occurred_at": occurred_at,
            "payment": _payment(row).model_dump(mode="json"),
        }
        connection.execute(
            update(events)
            .where(events.c.id == event_id)
            .values(body=json.dumps(body, default=_iso, separators=(",", ":")))
        )

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
                .where(
                    events.c.delivered_at.is_(None),
                    events.c.abandoned_at.is_(None),
                    events.c.next_attempt_at <= now,
                )
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


def _payment(row: Row[Any]) -> SimulatedPayment:
    return SimulatedPayment(
        id=f"simpay_{row.id}",
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
    )


def _internal_id(public_id: str) -> int:
    prefix, _, number = public_id.partition("_")
    if prefix != "simpay" or not number.isdigit():
        raise PaymentNotFound(public_id)
    return int(number)


def _event_id(internal: int) -> str:
    return f"simevt_{internal}"


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    raise TypeError(f"can't serialise {type(value).__name__}")
