"""What the Ledger records, as values: Accounts, Actors, Entries and Transactions (SPEC §3).

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


class NewAccount(_Value):
    code: AccountCode
    type: AccountType
    currency: Currency


class Account(NewAccount):
    recorded_at: datetime


class SystemJob(_Value):
    kind: Literal["system_job"] = "system_job"
    name: NonEmpty
    version: NonEmpty


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
    """A request to reverse a posted Transaction."""

    idempotency_key: IdempotencyKey
    actor: Actor
    effective_at: AwareDatetime
    description: Annotated[str, Field(max_length=500)] = ""


class Entry(_Value):
    id: int
    account: str
    currency: str
    amount: int
    effective_at: datetime
    recorded_at: datetime
    actor: Actor


class Transaction(_Value):
    id: int
    idempotency_key: str
    description: str
    recorded_at: datetime
    reverses: int | None
    reversed_by: int | None
    entries: list[Entry]


class Posted(_Value):
    """The outcome of a post or a Reversal. `replayed` is true when the Idempotency Key had
    already been posted with the same payload, and `transaction` is that original."""

    transaction: Transaction
    replayed: bool


def sums_by_currency(entries: list[NewEntry] | list[Entry]) -> dict[str, int]:
    sums: defaultdict[str, int] = defaultdict(int)
    for entry in entries:
        sums[entry.currency] += entry.amount
    return dict(sums)
