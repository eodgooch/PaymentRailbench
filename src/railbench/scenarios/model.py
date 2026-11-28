"""The Scenario file (SPEC §6, ADR 0011): a named, seeded script of Payments and Faults.

A Scenario is YAML, parsed into these models: strict (money is an integer in Minor Units,
an unknown key is an error) and checked as a whole (every Payment names a known Customer,
every Fault a known Payment and an event that Payment emits). `plans(seed)` resolves it into
the concrete `PaymentPlan`s the Simulator follows. All randomness (which events get random
Faults, and their parameters) is drawn here from one `random.Random(seed)` in a fixed order,
so the same file and seed give the same plans, and the Simulator makes no random choices.

Fault kinds are a discriminated union on `kind`, shared with the Simulator; Returns, Cutoff
and Settlement Report Faults join as new kinds (#7, #8) without changing this schema.
"""

import random
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from railbench.simulator.model import (
    DelayedDelivery,
    DuplicateDelivery,
    EventName,
    FaultPlan,
    Outcome,
    OutOfOrderDelivery,
    PaymentPlan,
    events_for,
)

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")]
RANDOM_PREFIX = "random-"


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Customer(_Strict):
    """A fictional Mintwharf Customer. Their USD Customer Balance is `customer:<id>:usd`."""

    id: Slug
    name: Annotated[str, Field(min_length=1, max_length=100)]

    def account(self, currency: str) -> str:
        return f"customer:{self.id}:{currency.lower()}"


class ScenarioPayment(_Strict):
    """An ACH pull funding a Customer Balance, originated `at` after the Scenario starts."""

    id: Slug
    customer: Slug
    amount: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3,5}$")] = "USD"
    at: timedelta = timedelta(0)
    outcome: Outcome = "settle"
    description: Annotated[str, Field(max_length=200)] = ""

    @model_validator(mode="after")
    def _not_before_the_start(self) -> Self:
        if self.at < timedelta(0):
            raise ValueError(f"Payment {self.id} can't start before the Scenario")
        return self


class _OnPayment(_Strict):
    payment: Slug


class Duplicate(DuplicateDelivery, _OnPayment):
    pass


class Delay(DelayedDelivery, _OnPayment):
    pass


class OutOfOrder(OutOfOrderDelivery, _OnPayment):
    pass


ScenarioFault = Annotated[Duplicate | Delay | OutOfOrder, Field(discriminator="kind")]
FaultKind = Literal["duplicate", "delay", "out_of_order"]


class RandomFaults(_Strict):
    """Give each Provider Event a Fault with probability `rate`, of a kind drawn from
    `kinds` (out-of-order only applies to an event that has a later one to wait for)."""

    rate: Annotated[float, Field(ge=0, le=1)]
    kinds: Annotated[tuple[FaultKind, ...], Field(min_length=1)]
    max_delay: timedelta = timedelta(hours=6)
    max_duplicates: Annotated[int, Field(strict=True, ge=1, le=10)] = 2


class Scenario(_Strict):
    name: Slug
    description: str = ""
    # The default seed; a run can be given another.
    seed: Annotated[int, Field(strict=True, ge=0)] = 0
    start: AwareDatetime
    customers: Annotated[tuple[Customer, ...], Field(min_length=1)]
    payments: Annotated[tuple[ScenarioPayment, ...], Field(min_length=1)]
    faults: tuple[ScenarioFault, ...] = ()
    random_faults: RandomFaults | None = None

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        for kind, ids in (
            ("Customer", [c.id for c in self.customers]),
            ("Payment", [p.id for p in self.payments]),
            ("Fault", [f.id for f in self.faults]),
        ):
            repeated = [found for found, count in Counter(ids).items() if count > 1]
            if repeated:
                raise ValueError(f"{kind} ids must be unique: {repeated}")
        customers = {c.id for c in self.customers}
        payments = {p.id for p in self.payments}
        for payment in self.payments:
            if payment.customer not in customers:
                raise ValueError(f"Payment {payment.id} names no known Customer")
        for fault in self.faults:
            if fault.payment not in payments:
                raise ValueError(f"Fault {fault.id} names no known Payment")
            if fault.id.startswith(RANDOM_PREFIX):
                raise ValueError(f"Fault ids starting {RANDOM_PREFIX!r} are kept for random ones")
        self.plans(self.seed)  # every Fault fits the Payment it's on
        return self

    def customer(self, customer_id: str) -> Customer:
        return next(c for c in self.customers if c.id == customer_id)

    def idempotency_key(self, payment: ScenarioPayment, seed: int) -> str:
        return f"scenario:{self.name}:{seed}:{payment.id}"

    def plans(self, seed: int) -> dict[str, PaymentPlan]:
        """The Simulator's plan for each Payment, by Payment id, for this seed."""
        rng = random.Random(seed)
        drawn = self._random_faults(rng)
        plans = {}
        for payment in self.payments:
            named: list[FaultPlan] = [
                _plain(fault) for fault in self.faults if fault.payment == payment.id
            ]
            plans[payment.id] = PaymentPlan(
                idempotency_key=self.idempotency_key(payment, seed),
                outcome=payment.outcome,
                faults=(*named, *drawn.get(payment.id, ())),
            )
        return plans

    def _random_faults(self, rng: random.Random) -> dict[str, list[FaultPlan]]:
        """Draw in a fixed order (Payments as listed, events as emitted) so a seed always
        draws the same Faults."""
        config = self.random_faults
        drawn: dict[str, list[FaultPlan]] = {}
        if config is None:
            return drawn
        count = 0
        for payment in self.payments:
            emitted = events_for(payment.outcome)
            for position, event in enumerate(emitted):
                if rng.random() >= config.rate:
                    continue
                later = emitted[position + 1 :]
                kinds = [k for k in config.kinds if k != "out_of_order" or later]
                if not kinds:
                    continue
                count += 1
                kind = rng.choice(kinds)
                fault_id = f"{RANDOM_PREFIX}{count:03d}"
                drawn.setdefault(payment.id, []).append(
                    _draw(rng, config, fault_id, kind, event, later)
                )
        return drawn


def _draw(
    rng: random.Random,
    config: RandomFaults,
    fault_id: str,
    kind: FaultKind,
    event: EventName,
    later: tuple[EventName, ...],
) -> FaultPlan:
    match kind:
        case "duplicate":
            times = rng.randint(1, config.max_duplicates)
            return DuplicateDelivery(id=fault_id, event=event, times=times)
        case "delay":
            minutes = rng.randint(1, max(1, int(config.max_delay / timedelta(minutes=1))))
            return DelayedDelivery(id=fault_id, event=event, by=timedelta(minutes=minutes))
        case "out_of_order":
            return OutOfOrderDelivery(id=fault_id, event=event, after=rng.choice(later))


def _plain(fault: ScenarioFault) -> FaultPlan:
    """The Simulator's view of a Scenario Fault: the same, without the Payment it's on."""
    data = fault.model_dump(exclude={"payment"})
    match fault:
        case Duplicate():
            return DuplicateDelivery(**data)
        case Delay():
            return DelayedDelivery(**data)
        case OutOfOrder():
            return OutOfOrderDelivery(**data)


def load(path: Path | str) -> Scenario:
    """Read a Scenario file."""
    return Scenario.model_validate(yaml.safe_load(Path(path).read_text()))
