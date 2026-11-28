"""The Scenario file (SPEC §6, ADR 0011): a named, seeded script of Payments and Faults.

A Scenario is YAML, parsed into these models: strict (money is an integer in Minor Units,
an unknown key is an error) and checked as a whole (every Payment names a known Customer,
every Fault a known Payment and an event that Payment emits). `plans(seed)` resolves it into
the concrete `PaymentPlan`s the Simulator follows. All randomness (which events get random
Faults, and their parameters) is drawn here from one `random.Random(seed)` in a fixed order,
so the same file and seed give the same plans, and the Simulator makes no random choices.

A Scenario can also close auctions (ADR 0012): each Auction Close lists its lots (buyer,
seller, hammer price) and, for the sellers it scripts, a Payout with its speed (`instant` or
`standard`) and how its Payments end. A Payout's id can be named by Faults like a Payment's.
Since the Ledger decides whether an Instant Payout goes out over RTP or falls back to ACH,
the Simulator gets a plan for each Payment the Payout could make (`<payout>:rtp` and
`<payout>:ach`), under the Idempotency Keys the Ledger will use, and a Fault lands on every
one of them that emits its event.

Fault kinds are a discriminated union on `kind`. Adding one takes four edits: its plan
model in the Simulator's `FaultPlan` union (`railbench.simulator.model`), the Simulator's
case for injecting it, a Scenario subclass here that adds the Payment it names (joined to
`ScenarioFault`), and its case in `_plain`; any checks it needs against other Faults or the
Payment's size go in `PaymentPlan`'s validators.

Settlement Report Faults (`missing_report_line`, `wrong_report_amount`,
`settlement_after_cutoff`, `duplicate_report_line`, `provider_adjustment`,
`orphan_adjustment`; ADR 0014, ADR 0015) name a
Payment or Payout that settles and act on its line in the Provider's Settlement Report
(an orphan adjustment adds a line for a Payment the Provider never made), never on a
Provider Event.
Their sizes are checked against what the Payment moves (a Payout's is its seller's proceeds
less Mintwharf's fee): a wrong amount stays above zero, an adjustment claws back at most what
settled, and only a Payout is recalled.

A `return` Fault returns an ACH pull with a NACHA return code, `at` after it settles
(ADR 0013). The Scenario knows when each pull settles (the Ledger originates it at `at` after
the start, and ACH timing is fixed by the calendar), so a Return outside its code's Return
Window is refused here, before anything runs; the Simulator checks again when it creates the
Payment. Only a pull that settles can be returned, never a Payout.
"""

import random
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from railbench.ledger.model import BIGINT_MAX
from railbench.marketplace.model import payout_key
from railbench.marketplace.service import seller_fee
from railbench.payments.accounts import customer_balance_account
from railbench.simulator.model import (
    DelayedDelivery,
    DuplicateDelivery,
    DuplicateReportLine,
    EventName,
    FaultPlan,
    MissingReportLine,
    OrphanAdjustment,
    Outcome,
    OutOfOrderDelivery,
    PaymentPlan,
    PlannedAdjustment,
    PlannedReturn,
    SettlementAfterCutoff,
    WrongReportAmount,
    events_for,
)
from railbench.simulator.provider import ach_settles_at

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")]
# The Ledger's seller fee when a Scenario doesn't set one (Settings.seller_fee_bps).
DEFAULT_SELLER_FEE_BPS = 1_000
RANDOM_PREFIX = "random-"


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Customer(_Strict):
    """A fictional Mintwharf Customer, with a Customer Balance per currency they use."""

    id: Slug
    name: Annotated[str, Field(min_length=1, max_length=100)]

    def account(self, currency: str) -> str:
        return customer_balance_account(self.id, currency)


class ScenarioPayment(_Strict):
    """An ACH pull funding a Customer Balance, originated `at` after the Scenario starts."""

    id: Slug
    customer: Slug
    amount: Annotated[int, Field(strict=True, gt=0, le=BIGINT_MAX)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3,5}$")] = "USD"
    at: timedelta = timedelta(0)
    outcome: Outcome = "settle"
    description: Annotated[str, Field(max_length=200)] = ""

    @model_validator(mode="after")
    def _not_before_the_start(self) -> Self:
        if self.at < timedelta(0):
            raise ValueError(f"Payment {self.id} can't start before the Scenario")
        return self


Price = Annotated[int, Field(strict=True, gt=0, le=BIGINT_MAX)]


class ScenarioLot(_Strict):
    id: Slug
    buyer: Slug
    seller: Slug
    hammer_price: Price


class ScenarioPayout(_Strict):
    """How one seller's Payout from an Auction Close goes: the speed they ask for, and how
    its RTP Payment (an Instant Payout) and ACH Payment (a Standard Payout, or an Instant one
    that fell back) end."""

    id: Slug
    seller: Slug
    speed: Literal["instant", "standard"] = "standard"
    rtp_outcome: Literal["settle", "decline"] = "settle"
    ach_outcome: Outcome = "settle"

    def rails(self) -> tuple[Literal["rtp", "ach"], ...]:
        return ("rtp", "ach") if self.speed == "instant" else ("ach",)


class ScenarioAuctionClose(_Strict):
    """An auction closing `at` after the Scenario starts. Sellers with no `payouts` entry get
    a Standard Payout that settles."""

    id: Slug
    at: timedelta = timedelta(0)
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3,5}$")] = "USD"
    lots: Annotated[tuple[ScenarioLot, ...], Field(min_length=1)]
    payouts: tuple[ScenarioPayout, ...] = ()


class _OnPayment(_Strict):
    payment: Slug


class Duplicate(DuplicateDelivery, _OnPayment):
    pass


class Delay(DelayedDelivery, _OnPayment):
    pass


class OutOfOrder(OutOfOrderDelivery, _OnPayment):
    pass


class Return(PlannedReturn, _OnPayment):
    pass


class MissingLine(MissingReportLine, _OnPayment):
    pass


class WrongAmount(WrongReportAmount, _OnPayment):
    pass


class AfterCutoff(SettlementAfterCutoff, _OnPayment):
    pass


class ListedTwice(DuplicateReportLine, _OnPayment):
    pass


class Adjustment(PlannedAdjustment, _OnPayment):
    pass


class Orphan(OrphanAdjustment, _OnPayment):
    """An orphan adjustment, on the report of the Banking Day `after` the named Payment's."""


ScenarioFault = Annotated[
    Duplicate
    | Delay
    | OutOfOrder
    | Return
    | MissingLine
    | WrongAmount
    | AfterCutoff
    | ListedTwice
    | Adjustment
    | Orphan,
    Field(discriminator="kind"),
]
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
    payments: tuple[ScenarioPayment, ...] = ()
    auction_closes: tuple[ScenarioAuctionClose, ...] = ()
    faults: tuple[ScenarioFault, ...] = ()
    random_faults: RandomFaults | None = None
    # The Ledger's settings for the run, when the Scenario needs other than the defaults.
    instant_payout_limit: Annotated[int, Field(strict=True, ge=0)] | None = None
    seller_fee_bps: Annotated[int, Field(strict=True, ge=0, lt=10_000)] | None = None

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        if not self.payments and not self.auction_closes:
            raise ValueError("a Scenario needs at least one Payment or Auction Close")
        payouts = [p for close in self.auction_closes for p in close.payouts]
        for kind, ids in (
            ("Customer", [c.id for c in self.customers]),
            ("Payment or Payout", [p.id for p in self.payments] + [p.id for p in payouts]),
            ("Auction Close", [a.id for a in self.auction_closes]),
            ("Fault", [f.id for f in self.faults]),
        ):
            repeated = [found for found, count in Counter(ids).items() if count > 1]
            if repeated:
                raise ValueError(f"{kind} ids must be unique: {repeated}")
        customers = {c.id for c in self.customers}
        payments = {p.id for p in self.payments} | {p.id for p in payouts}
        for payment in self.payments:
            if payment.customer not in customers:
                raise ValueError(f"Payment {payment.id} names no known Customer")
        for close in self.auction_closes:
            for lot in close.lots:
                if {lot.buyer, lot.seller} - customers:
                    raise ValueError(f"lot {lot.id} names an unknown Customer")
                if lot.buyer == lot.seller:
                    raise ValueError(f"lot {lot.id}: a Customer can't buy their own lot")
            sellers = {lot.seller for lot in close.lots}
            for payout in close.payouts:
                if payout.seller not in sellers:
                    raise ValueError(f"Payout {payout.id}: {payout.seller} sold nothing")
            if len({p.seller for p in close.payouts}) < len(close.payouts):
                raise ValueError(f"Auction Close {close.id} scripts a seller's Payout twice")
        for fault in self.faults:
            if fault.payment not in payments:
                raise ValueError(f"Fault {fault.id} names no known Payment or Payout")
            if fault.id.startswith(RANDOM_PREFIX):
                raise ValueError(f"Fault ids starting {RANDOM_PREFIX!r} are kept for random ones")
            if isinstance(fault, Return):
                self._check_return(fault)
        self._check_sizes(self.plans(self.seed))  # every Fault fits the Payment it's on
        return self

    def _check_sizes(self, plans: dict[str, PaymentPlan]) -> None:
        moves = {p.id: ("pull", p.amount) for p in self.payments}
        fee_bps = DEFAULT_SELLER_FEE_BPS if self.seller_fee_bps is None else self.seller_fee_bps
        for close in self.auction_closes:
            for payout in close.payouts:
                owed = sum(
                    lot.hammer_price - seller_fee(lot.hammer_price, fee_bps)
                    for lot in close.lots
                    if lot.seller == payout.seller
                )
                for rail in payout.rails():
                    moves[f"{payout.id}:{rail}"] = ("push", owed)
        for name, plan in plans.items():
            direction, amount = moves[name]
            plan.check_against(direction, amount)

    def _check_return(self, fault: "Return") -> None:
        pull = next((p for p in self.payments if p.id == fault.payment), None)
        if pull is None:
            raise ValueError(f"Fault {fault.id}: only an ACH pull is returned, not a Payout")
        settles_at = ach_settles_at(self.start + pull.at)
        if not fault.code.within_window(settles_at, settles_at + fault.at):
            raise ValueError(
                f"Fault {fault.id}: a {fault.code.code} Return {fault.at} after settlement is "
                f"outside its Return Window (until {fault.code.deadline(settles_at)})"
            )

    def customer(self, customer_id: str) -> Customer:
        return next(c for c in self.customers if c.id == customer_id)

    def idempotency_key(self, payment: ScenarioPayment, seed: int) -> str:
        return f"scenario:{self.name}:{seed}:{payment.id}"

    def auction_id(self, close: ScenarioAuctionClose, seed: int) -> str:
        return f"{self.name}:{seed}:{close.id}"

    def instant_payouts(self, close: ScenarioAuctionClose) -> list[str]:
        """The Customer Balances of the sellers asking for an Instant Payout."""
        return sorted(
            self.customer(p.seller).account(close.currency)
            for p in close.payouts
            if p.speed == "instant"
        )

    def plans(self, seed: int) -> dict[str, PaymentPlan]:
        """The Simulator's plan for each Payment, by Payment id, for this seed, and for each
        Payment a scripted Payout could make, by `<payout id>:<rail>`."""
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
        for close in self.auction_closes:
            for payout in close.payouts:
                plans |= self._payout_plans(close, payout, seed)
        return plans

    def _payout_plans(
        self, close: ScenarioAuctionClose, payout: ScenarioPayout, seed: int
    ) -> dict[str, PaymentPlan]:
        account = self.customer(payout.seller).account(close.currency)
        found: dict[str, PaymentPlan] = {}
        landed: set[str] = set()
        for rail in payout.rails():
            outcome = payout.rtp_outcome if rail == "rtp" else payout.ach_outcome
            emitted = events_for(outcome, rail)
            faults = [
                _plain(fault)
                for fault in self.faults
                if fault.payment == payout.id
                and fault.event in emitted
                and (not isinstance(fault, OutOfOrder) or fault.after in emitted)
            ]
            landed |= {fault.id for fault in faults}
            found[f"{payout.id}:{rail}"] = PaymentPlan(
                idempotency_key=payout_key(self.auction_id(close, seed), account, rail),
                rail=rail,
                outcome=outcome,
                faults=tuple(faults),
            )
        for fault in self.faults:
            if fault.payment == payout.id and fault.id not in landed:
                raise ValueError(
                    f"fault {fault.id}: Payout {payout.id} never emits {fault.event!r} on any "
                    "Rail it can go out on"
                )
        return found

    def _random_faults(self, rng: random.Random) -> dict[str, list[FaultPlan]]:
        """Draw in a fixed order (Payments as listed, events as emitted) so a seed always
        draws the same Faults."""
        config = self.random_faults
        drawn: dict[str, list[FaultPlan]] = {}
        if config is None:
            return drawn
        count = 0
        for payment in self.payments:
            emitted = events_for(payment.outcome, "ach")
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
        case Return():
            return PlannedReturn(**data)
        case MissingLine():
            return MissingReportLine(**data)
        case WrongAmount():
            return WrongReportAmount(**data)
        case AfterCutoff():
            return SettlementAfterCutoff(**data)
        case ListedTwice():
            return DuplicateReportLine(**data)
        case Adjustment():
            return PlannedAdjustment(**data)
        case Orphan():
            return OrphanAdjustment(**data)


def load(path: Path | str) -> Scenario:
    """Read a Scenario file."""
    return Scenario.model_validate(yaml.safe_load(Path(path).read_text()))
