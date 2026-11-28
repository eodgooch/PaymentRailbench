"""What the Reconciliation API accepts and returns (SPEC §7.1, ADR 0015)."""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from railbench.ledger.model import NonEmpty
from railbench.reconciliation.matching import BreakType


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BreakStatus(StrEnum):
    OPEN = "open"  # in the Exception Queue
    RESOLVED = "resolved"


class EvidenceView(_Value):
    """The rows a Break rests on, by their database ids: `GET /payments/{id}` for a Payment,
    `GET /transactions/{id}` for a Ledger Transaction, and so on."""

    payments: list[int] = []
    transitions: list[int] = []
    inbox: list[int] = []
    transactions: list[int] = []
    reports: list[int] = []
    report_lines: list[int] = []
    provider_adjustments: list[int] = []


class Resolver(_Value):
    """The person resolving a Break: an operator, and the approved Proposal they acted on,
    if they acted on one. Not an Entry's person Actor (operator and approved Proposal, both
    required): resolving a Break moves no money, and a person may resolve one by their own
    judgement, with a reason. A system job never resolves a Break (ADR 0015)."""

    kind: Literal["person"] = "person"
    operator_id: NonEmpty
    proposal_id: NonEmpty | None = None


class NewResolution(_Value):
    """How a Break was resolved: by a correcting Transaction already posted to the Ledger
    (through the Journal, by deterministic code), or with a reason. Exactly one."""

    actor: Resolver
    correcting_transaction_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    reason: Annotated[str, Field(min_length=1, max_length=1000)] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.correcting_transaction_id is None) == (self.reason is None):
            raise ValueError("a resolution names a correcting Transaction or a reason, not both")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("a reason can't be blank")
        return self


class Resolution(NewResolution):
    recorded_at: datetime


class Break(_Value):
    id: str
    provider: str
    banking_day: date
    break_type: BreakType
    match_key: str
    exposure: int  # Minor Units of `currency`, never negative
    currency: str
    facts: dict[str, Any]
    evidence: EvidenceView
    # Other Breaks with the same cause: those this one names (`facts.related_breaks`, e.g.
    # the missing-at-Provider Break of a line that turned up more than a day late) and those
    # that name it.
    related_breaks: list[str] = []
    detected_at: datetime  # when Reconciliation found it (the run's Recorded Time)
    status: BreakStatus
    # From when the Banking Day it concerns was due on its report (20:00 New York) until
    # now (open) or until its resolution (resolved): older Banking Days rank first.
    age_seconds: int
    resolution: Resolution | None


class RunView(_Value):
    """One Reconciliation of a Provider's Banking Day."""

    provider: str
    banking_day: date
    report_id: int
    next_report_id: int
    known_at: datetime  # it matched what the Ledger knew at this moment
    job: str
    recorded_at: datetime
    breaks: list[str]  # Break ids, in Break Type then match key order


class DayStatus(_Value):
    """Where one Provider's Banking Day stands in Reconciliation: what Daily Close asks."""

    provider: str
    banking_day: date
    reconciled: bool
    breaks: int
    open_breaks: int
    # Total exposure of the open Breaks, per currency, in Minor Units.
    open_exposure: dict[str, int]
