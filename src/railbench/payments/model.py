"""What the Payments API accepts and returns. Money is a strict integer in Minor Units."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from railbench.ledger.model import BIGINT_MAX, AccountCode, Currency, IdempotencyKey
from railbench.payments.state_machine import AchState


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NewPayment(_Value):
    """An ACH pull from the Customer's bank into their Customer Balance."""

    idempotency_key: IdempotencyKey
    rail: Literal["ach"]
    direction: Literal["pull"]
    customer_account: AccountCode
    amount: Annotated[int, Field(strict=True, gt=0, le=BIGINT_MAX)]
    currency: Currency
    description: Annotated[str, Field(max_length=200)] = ""


class Transition(_Value):
    """One state change. Its Actor is the Inbox event behind it or a system job."""

    state: AchState
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


class Payment(_Value):
    id: int
    idempotency_key: str
    provider: str
    provider_payment_id: str
    rail: str
    direction: str
    customer_account: str
    currency: str
    amount: int
    description: str
    recorded_at: datetime
    state: AchState
    # When a settled ACH Payment becomes Available: the start (midnight, New York) of the
    # second Banking Day after the Banking Day it settled on (ADR 0004, ADR 0010).
    available_at: datetime | None
    transitions: list[Transition]
    exceptions: list[PaymentException]


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
