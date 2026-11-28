"""The Simulator's wire format: what a Provider's API accepts and returns.

The Ledger's HTTP adapter speaks this format with its own models; it never imports these
(ADR 0005).
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

BIGINT_MAX = 2**63 - 1


class PaymentRequest(BaseModel):
    """Create a Payment. Only ACH pulls for now; RTP joins with Payouts (#6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    rail: Literal["ach"]
    direction: Literal["pull"]
    amount: Annotated[int, Field(strict=True, gt=0, le=BIGINT_MAX)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3,5}$")]
    reference: Annotated[str, Field(max_length=200)] = ""


class SimulatedPayment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    idempotency_key: str
    rail: str
    direction: str
    currency: str
    amount: int
    reference: str
    status: Literal["submitted", "settled"]
    created_at: datetime
    submitted_at: datetime | None
    settled_at: datetime | None
