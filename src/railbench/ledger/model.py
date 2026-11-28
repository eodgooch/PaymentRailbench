"""What the Ledger records, as values: Accounts, Actors, Entries, Transactions, Settlements,
Holds and Balances (SPEC §3).

Money is a strict integer in Minor Units: a float, a numeric string or a bool is rejected,
never coerced. Requests are validated here, so the Journal and the HTTP API share one set of
rules; the database enforces the same rules again (migration 0002).
"""

from collections import defaultdict
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

BIGINT_MAX = 2**63 - 1

Currency = Annotated[str, Field(pattern=r"^[A-Z]{3,5}$", examples=["USD", "USDC"])]
AccountCode = Annotated[
    str, Field(pattern=r"^[a-z0-9][a-z0-9:._-]{0,127}$", examples=["customer:c-1001:usd"])
]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=200)]
NonEmpty = Annotated[str, Field(min_length=1, max_length=200)]


def _not_zero(amount: int) -> int:
    if amount == 0:
        raise ValueError("an Entry's amount can't be zero")
    return amount


MinorUnits = Annotated[
    int,
    Field(strict=True, ge=-BIGINT_MAX, le=BIGINT_MAX, description="Signed integer Minor Units"),
    AfterValidator(_not_zero),
]


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    REVENUE = "revenue"
    EXPENSE = "expense"

    @property
    def normal_sign(self) -> int:
        """+1 for debit-normal Accounts (asset, expense), -1 for credit-normal ones (liability,
        revenue). A Balance reads as the raw debit-positive sum times this sign, so a Customer
        Balance holding money Mintwharf owes the Customer is positive (ADR 0009)."""
        return 1 if self in (AccountType.ASSET, AccountType.EXPENSE) else -1


class TransactionStatus(StrEnum):
    """Fixed when the Transaction is posted. A pending Transaction's Entries are money in
    motion until a Settlement turns them Posted (ADR 0009)."""

    PENDING = "pending"
    POSTED = "posted"


class NewAccount(_Value):
    code: AccountCode
    type: AccountType
    currency: Currency


class Account(NewAccount):
    recorded_at: datetime


def job_label(name: str, version: str) -> str:
    """How a system job is named in views and logs: `<name>@<version>`."""
    return f"{name}@{version}"


class SystemJob(_Value):
    kind: Literal["system_job"] = "system_job"
    name: NonEmpty
    version: NonEmpty

    @property
    def label(self) -> str:
        return job_label(self.name, self.version)


class ProviderEventActor(_Value):
    kind: Literal["provider_event"] = "provider_event"
    inbox_id: NonEmpty


class Person(_Value):
    """An operator, acting on an approved Proposal."""

    kind: Literal["person"] = "person"
    operator_id: NonEmpty
    proposal_id: NonEmpty


Actor = Annotated[SystemJob | ProviderEventActor | Person, Field(discriminator="kind")]


class NewEntry(_Value):
    account: AccountCode
    currency: Currency
    amount: MinorUnits


class NewTransaction(_Value):
    """A Transaction to post. Its Entries must sum to zero in each currency."""

    idempotency_key: IdempotencyKey
    actor: Actor
    effective_at: AwareDatetime
    description: Annotated[str, Field(max_length=500)] = ""
    status: TransactionStatus = TransactionStatus.POSTED
    entries: Annotated[list[NewEntry], Field(min_length=2)]

    @model_validator(mode="after")
    def _balances(self) -> Self:
        unbalanced = {
            currency: total for currency, total in sums_by_currency(self.entries).items() if total
        }
        if unbalanced:
            detail = ", ".join(f"{c} {t:+d}" for c, t in sorted(unbalanced.items()))
            raise ValueError(f"Entries must sum to zero in each currency; off by {detail}")
        return self


class ReversalRequest(_Value):
    """A request to reverse a Transaction, posted or pending (ADR 0009)."""

    idempotency_key: IdempotencyKey
    actor: Actor
    effective_at: AwareDatetime
    description: Annotated[str, Field(max_length=500)] = ""


class SettlementRequest(_Value):
    """A request to settle a pending Transaction: its Entries count as Posted from
    `effective_at`, as known from when the Ledger records the Settlement."""

    idempotency_key: IdempotencyKey
    actor: Actor
    effective_at: AwareDatetime


class Entry(_Value):
    id: int
    account: str
    currency: str
    amount: int
    effective_at: datetime
    recorded_at: datetime
    actor: Actor


class Settlement(_Value):
    id: int
    effective_at: datetime
    recorded_at: datetime
    actor: Actor


class Transaction(_Value):
    id: int
    idempotency_key: str
    description: str
    recorded_at: datetime
    status: TransactionStatus
    reverses: int | None
    reversed_by: int | None
    settlement: Settlement | None
    entries: list[Entry]


class Posted(_Value):
    """The outcome of a post or a Reversal. `replayed` is true when the Idempotency Key had
    already been posted with the same payload, and `transaction` is that original."""

    transaction: Transaction
    replayed: bool


class Balances(_Value):
    """An Account's balances at an Effective Time, as known at a Recorded Time, in its normal
    direction (ADR 0009). Always derived, never stored.

    `available` = `posted` - `held` - `unsettled_outflows`, where `held` is the total of
    open Holds and `unsettled_outflows` is what pending Transactions are taking out of the
    Account (each counted net of its Reversal, and only when it reduces the balance).
    """

    account: str
    currency: str
    effective_at: datetime
    known_at: datetime
    posted: int
    pending: int
    held: int
    unsettled_outflows: int
    available: int


HoldAmount = Annotated[
    int, Field(strict=True, gt=0, le=BIGINT_MAX, description="Positive integer Minor Units")
]


class NewHold(_Value):
    """A Hold to place: it reserves `amount` of the Account's Available Balance without
    moving money."""

    idempotency_key: IdempotencyKey
    actor: Actor
    effective_at: AwareDatetime
    account: AccountCode
    currency: Currency
    amount: HoldAmount
    description: Annotated[str, Field(max_length=500)] = ""


class ReleaseRequest(_Value):
    """A request to release a Hold, ending the reservation with no money moved."""

    idempotency_key: IdempotencyKey
    actor: Actor
    effective_at: AwareDatetime


class HoldOutcome(StrEnum):
    RELEASED = "released"
    CAPTURED = "captured"


class HoldEnd(_Value):
    outcome: HoldOutcome
    transaction: int | None  # the capturing Transaction
    effective_at: datetime
    recorded_at: datetime
    actor: Actor


class Hold(_Value):
    id: int
    idempotency_key: str
    account: str
    currency: str
    amount: int
    description: str
    effective_at: datetime
    recorded_at: datetime
    actor: Actor
    ended: HoldEnd | None


class HoldRecorded(_Value):
    """The outcome of placing or releasing a Hold. `replayed` is true for a retry."""

    hold: Hold
    replayed: bool


def sums_by_currency(entries: list[NewEntry] | list[Entry]) -> dict[str, int]:
    sums: defaultdict[str, int] = defaultdict(int)
    for entry in entries:
        sums[entry.currency] += entry.amount
    return dict(sums)
