"""The Simulator's wire format: what a Provider's API accepts and returns.

The Ledger's HTTP adapter speaks this format with its own models; it never imports these
(ADR 0005).
"""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from railbench.common.returns import RETURN_CODES

BIGINT_MAX = 2**63 - 1

Rail = Literal["ach", "rtp"]
Direction = Literal["pull", "push"]


class PaymentStatus(StrEnum):
    SUBMITTED = "submitted"
    SETTLED = "settled"
    DECLINED = "declined"
    CANCELED = "canceled"
    RETURNED = "returned"


class EventType(StrEnum):
    """The Provider Events the Simulator sends, one per status change."""

    SUBMITTED = "payment.submitted"
    SETTLED = "payment.settled"
    DECLINED = "payment.declined"
    CANCELED = "payment.canceled"
    RETURNED = "payment.returned"


class PaymentRequest(BaseModel):
    """Create a Payment: an ACH pull or push, or an RTP push (RTP is credit push only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    rail: Rail
    direction: Direction
    amount: Annotated[int, Field(strict=True, gt=0, le=BIGINT_MAX)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3,5}$")]
    reference: Annotated[str, Field(max_length=200)] = ""

    @model_validator(mode="after")
    def _rtp_pushes_only(self) -> Self:
        if self.rail == "rtp" and self.direction != "push":
            raise ValueError("RTP is credit push only: it can't pull money")
        return self


class SimulatedPayment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    idempotency_key: str
    rail: Rail
    direction: Direction
    currency: str
    amount: int
    reference: str
    status: PaymentStatus
    created_at: datetime
    submitted_at: datetime | None
    settled_at: datetime | None
    # When it was declined or canceled, if it was.
    ended_at: datetime | None = None
    # When it was returned, if it was, and with which NACHA return code.
    returned_at: datetime | None = None
    return_code: str | None = None


class ReturnRequest(BaseModel):
    """Return a settled ACH pull now, with a NACHA return code (a Return injected by hand)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str


# Scripting the Simulator (SPEC §6): how a Payment ends and which delivery Faults to inject.
# A Scenario resolves its randomness into these concrete plans before the run, so the
# Simulator itself makes no random choices.

Outcome = Literal["settle", "decline", "cancel"]
EventName = Literal["submitted", "settled", "declined", "canceled", "returned"]
FaultId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,79}$")]

FINAL_EVENT: dict[str, EventName] = {
    "settle": "settled",
    "decline": "declined",
    "cancel": "canceled",
}
STATUS_FOR_OUTCOME = {
    "settle": PaymentStatus.SETTLED,
    "decline": PaymentStatus.DECLINED,
    "cancel": PaymentStatus.CANCELED,
}


def events_for(outcome: Outcome, rail: Rail, returned: bool = False) -> tuple[EventName, ...]:
    """The Provider Events a Payment on `rail` with this outcome emits, in the order it
    emits them, ending with its Return if it's `returned`. An RTP Payment emits only its
    ending: it settles or is declined at once."""
    if rail == "rtp":
        return (FINAL_EVENT[outcome],)
    ending: tuple[EventName, ...] = ("returned",) if returned else ()
    return ("submitted", FINAL_EVENT[outcome], *ending)


class _Fault(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: FaultId
    event: EventName


class DuplicateDelivery(_Fault):
    """Deliver the event `times` more times after the first: same event ID, same body. With
    `after`, the extra copies are held until that later event has been delivered (a
    redelivery long after the original, say a settlement redelivered after its Return)."""

    kind: Literal["duplicate"] = "duplicate"
    times: Annotated[int, Field(strict=True, ge=1, le=10)] = 1
    after: EventName | None = None


class DelayedDelivery(_Fault):
    """Hold the event back for `by` (on the Clock) past when it was due."""

    kind: Literal["delay"] = "delay"
    by: timedelta

    @model_validator(mode="after")
    def _positive(self) -> Self:
        if self.by <= timedelta(0):
            raise ValueError("a delay must be positive")
        return self


class OutOfOrderDelivery(_Fault):
    """Hold the event back until a later event of the same Payment has been delivered."""

    kind: Literal["out_of_order"] = "out_of_order"
    after: EventName


class PlannedReturn(_Fault):
    """Return the settled ACH pull with NACHA return `code`, `at` after it settled. It must
    land inside the code's Return Window (`railbench.common.returns`): the Simulator refuses
    the Payment otherwise, once it knows when it settles. A Late Return is one after the
    funds became Available (SPEC §6: "Late Return, any code, any time inside its window")."""

    kind: Literal["return"] = "return"
    event: Literal["returned"] = "returned"
    code: str
    at: timedelta

    @field_validator("code")
    @classmethod
    def _known_code(cls, code: str) -> str:
        if code not in RETURN_CODES:
            raise ValueError(f"the Simulator knows no NACHA return code {code!r}")
        return code

    @model_validator(mode="after")
    def _after_settlement(self) -> Self:
        if self.at < timedelta(0):
            raise ValueError("a Return can't come before its Payment settled")
        return self


# A discriminated union: Cutoff and Settlement Report Faults join as new kinds.
FaultPlan = Annotated[
    DuplicateDelivery | DelayedDelivery | OutOfOrderDelivery | PlannedReturn,
    Field(discriminator="kind"),
]


class PaymentPlan(BaseModel):
    """What the Simulator will do with the Payment created under `idempotency_key`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    rail: Rail = "ach"
    outcome: Outcome = "settle"
    faults: tuple[FaultPlan, ...] = ()

    @property
    def planned_return(self) -> "PlannedReturn | None":
        return next((f for f in self.faults if isinstance(f, PlannedReturn)), None)

    @model_validator(mode="after")
    def _faults_fit_the_outcome(self) -> Self:
        if self.rail == "rtp" and self.outcome == "cancel":
            raise ValueError("an RTP Payment can't be canceled: it settles or is declined")
        returns = [fault for fault in self.faults if isinstance(fault, PlannedReturn)]
        if returns and (self.rail != "ach" or self.outcome != "settle"):
            raise ValueError("only an ACH Payment that settles can be returned (RTP has none)")
        if len(returns) > 1:
            raise ValueError("a Payment is returned at most once")
        emitted = events_for(self.outcome, self.rail, returned=bool(returns))
        for fault in self.faults:
            later = (
                fault.after if isinstance(fault, OutOfOrderDelivery | DuplicateDelivery) else None
            )
            if fault.event not in emitted:
                raise ValueError(
                    f"fault {fault.id}: a Payment that ends {self.outcome!r} never emits "
                    f"{fault.event!r}"
                )
            if later is not None and (
                later not in emitted or emitted.index(later) <= emitted.index(fault.event)
            ):
                raise ValueError(
                    f"fault {fault.id}: {later!r} is not emitted after {fault.event!r}"
                )
        return self


class InjectedFault(BaseModel):
    """One line of the Fault Manifest: a Fault the Simulator injected and its true root
    cause. Only Scenario tooling and the triage evaluation read these; the Ledger never does
    (its database role can't reach the `simulator` schema)."""

    model_config = ConfigDict(frozen=True)

    fault_id: str
    kind: str
    payment_id: str
    idempotency_key: str
    event_id: str
    event_type: str
    params: dict[str, Any]
    injected_at: datetime
    cause_code: str
    root_cause: str
