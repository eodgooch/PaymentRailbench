"""What an Auction Close accepts and returns (SPEC §4.4, ADR 0012). Money is a strict integer
in Minor Units; Customers are named by their Customer Balance."""

from collections import Counter
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from railbench.ledger.model import BIGINT_MAX, AccountCode, Currency

AuctionId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9:._-]{0,119}$")]
LotId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,39}$")]
Speed = Literal["instant", "standard"]
Step = Literal["instant", "held", "released"]
KEY_LIMIT = 200


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NewLot(_Value):
    lot_id: LotId
    buyer: AccountCode
    seller: AccountCode
    hammer_price: Annotated[int, Field(strict=True, gt=0, le=BIGINT_MAX)]

    @model_validator(mode="after")
    def _two_customers(self) -> Self:
        if self.buyer == self.seller:
            raise ValueError(f"lot {self.lot_id}: a Customer can't buy their own lot")
        return self


def payout_key(auction_id: str, seller: str, step: str) -> str:
    """The Idempotency Key of a Ledger row or Payment one seller's Payout makes: its Hold
    (`hold`), the Hold's release (`release`), its RTP or ACH Payment (`rtp`, `ach`)."""
    return f"auction:{auction_id}:payout:{seller}:{step}"


def lot_key(auction_id: str, lot_id: str) -> str:
    """The Idempotency Key of the Transaction that books one lot."""
    return f"auction:{auction_id}:lot:{lot_id}"


class NewAuctionClose(_Value):
    """One auction's close: every lot sold, and which sellers asked for an Instant Payout
    (everyone else gets a Standard Payout). All in one currency."""

    auction_id: AuctionId
    closed_at: AwareDatetime
    currency: Currency
    lots: Annotated[list[NewLot], Field(min_length=1, max_length=5_000)]
    instant_payouts: list[AccountCode] = []

    @field_validator("instant_payouts")
    @classmethod
    def _canonical(cls, sellers: list[str]) -> list[str]:
        """Sorted and unique: the order they're asked in means nothing."""
        return sorted(set(sellers))

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        repeated = [lot for lot, n in Counter(lot.lot_id for lot in self.lots).items() if n > 1]
        if repeated:
            raise ValueError(f"lot ids must be unique: {repeated}")
        sellers = {lot.seller for lot in self.lots}
        not_selling = sorted(set(self.instant_payouts) - sellers)
        if not_selling:
            raise ValueError(f"Instant Payouts asked for Customers who sold nothing: {not_selling}")
        longest = max(len(payout_key(self.auction_id, seller, "release")) for seller in sellers)
        if longest > KEY_LIMIT:
            raise ValueError("auction_id and seller Account codes too long to key their Payouts")
        return self


class Lot(_Value):
    lot_id: str
    buyer: str
    seller: str
    hammer_price: int
    fee: int
    transaction_id: int


class PayoutStep(_Value):
    step: Step
    detail: str
    hold_id: int | None
    job: str
    effective_at: datetime
    recorded_at: datetime


class PayoutPayment(_Value):
    """A Payment a Payout made: its RTP push, or its ACH push once released."""

    payment_id: int
    rail: str
    state: str


class Payout(_Value):
    id: int
    customer_account: str
    currency: str
    amount: int
    requested: Speed
    # What it is now: an Instant Payout over the limit, or declined, became Standard.
    speed: Speed
    steps: list[PayoutStep]
    payments: list[PayoutPayment]


class CustomerPosition(_Value):
    """One Customer's side of the Auction Close, gross: what they owe for lots they bought,
    and what they sold, less Mintwharf's fees, which is what they're owed and paid out."""

    customer_account: str
    owes: int
    sold: int
    fees: int
    owed: int


class AuctionClose(_Value):
    auction_id: str
    closed_at: datetime
    currency: str
    seller_fee_bps: int
    instant_payout_limit: int
    recorded_at: datetime
    lots: list[Lot]
    customers: list[CustomerPosition]
    payouts: list[Payout]


class AuctionCloseRecorded(_Value):
    auction_close: AuctionClose
    replayed: bool
