"""What the Payments API accepts and returns. Money is a strict integer in Minor Units."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from railbench.common.returns import NachaReturnCode
from railbench.ledger.model import BIGINT_MAX, AccountCode, Currency, IdempotencyKey
from railbench.payments.state_machine import Direction, PaymentState, Rail


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NewPayment(_Value):
    """A pull from the Customer's bank into their Customer Balance (ACH), or a push out of
    it to the Customer's bank, a Payout (ACH or RTP; RTP is credit push only)."""

    idempotency_key: IdempotencyKey
    rail: Rail
    direction: Direction
    customer_account: AccountCode
    amount: Annotated[int, Field(strict=True, gt=0, le=BIGINT_MAX)]
    currency: Currency
    description: Annotated[str, Field(max_length=200)] = ""
    # A push may name the Hold reserving its amount on the Customer Balance: the Transaction
    # that books the money going out captures it, so the money is never Available on its
    # way out (ADR 0012).
    hold_id: Annotated[int, Field(strict=True, gt=0)] | None = None

    @model_validator(mode="after")
    def _rtp_pushes_only(self) -> Self:
        if self.rail == "rtp" and self.direction != "push":
            raise ValueError("RTP is credit push only: it can't pull money")
        if self.hold_id is not None and self.direction != "push":
            raise ValueError("only a push reserves its amount with a Hold")
        return self


class Transition(_Value):
    """One state change. Its Actor is the Inbox event behind it or a system job."""

    state: PaymentState
    effective_at: datetime
    recorded_at: datetime
    inbox_id: int | None
    job: str | None
    transaction_id: int | None
    hold_id: int | None


class Outcome(StrEnum):
    APPLIED = "applied"
    STALE = "stale"  # a state already passed: recorded, nothing done
    EXCEPTION = "exception"  # an impossible transition: recorded, no Ledger write


class PaymentException(_Value):
    inbox_id: int
    detail: str
    recorded_at: datetime


class ReturnException(_Value):
    """A Return left the Customer Balance short: the deficit (`exposure`, in Minor Units) was
    moved to the Customer's Recovery Receivable by `recovery_transaction_id`. Raised by the
    Provider Event `inbox_id`; `late` if the funds had been Available (a Late Return), so
    Mintwharf may have paid them out. An exception for people to work, not a Reconciliation
    Break (SPEC §4.3, ADR 0013)."""

    payment_id: int
    inbox_id: int
    customer_account: str
    recovery_account: str
    currency: str
    return_code: NachaReturnCode
    exposure: int
    late: bool
    recovery_transaction_id: int
    detail: str
    effective_at: datetime
    recorded_at: datetime


class Payment(_Value):
    id: int
    idempotency_key: str
    provider: str
    provider_payment_id: str
    rail: Rail
    direction: Direction
    customer_account: str
    currency: str
    amount: int
    description: str
    recorded_at: datetime
    hold_id: int | None
    state: PaymentState
    # When a settled ACH pull becomes Available: the start (midnight, New York) of the
    # second Banking Day after the Banking Day it settled on (ADR 0004, ADR 0010).
    available_at: datetime | None
    transitions: list[Transition]
    exceptions: list[PaymentException]
    return_exceptions: list[ReturnException]


class PaymentRef(_Value):
    """A Payment's identity and where it stands, without its history."""

    id: int
    idempotency_key: str
    rail: Rail
    direction: Direction
    state: PaymentState


class PaymentCreated(_Value):
    payment: Payment
    replayed: bool


class Receipt(_Value):
    """What receiving one webhook did."""

    inbox_id: int
    event_id: str
    duplicate: bool  # this event ID was already in the Inbox
    outcome: Outcome
    detail: str = ""
