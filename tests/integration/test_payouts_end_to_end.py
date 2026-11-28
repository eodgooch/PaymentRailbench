"""Auction Close and Payouts end to end (SPEC §4.1, §4.4, ADR 0004, ADR 0012).

At an Auction Close the Ledger books what each buyer owes and each seller is owed, lot by
lot, with Mintwharf's fee, and makes each seller's Payout: a Standard Payout over ACH once
the buyers' funds behind it are Available, or an Instant Payout over RTP at once, at
Mintwharf's risk, within the seller's risk limit. Both services run in-process on one
ManualClock and talk over real HTTP, as in the ACH pull's end-to-end test.

The buyers fund by ACH pull on Thursday 3 September 2026; it settles Friday, before Labor
Day, and is Available on Wednesday 9 September.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select

from railbench.common.clock import ManualClock
from railbench.common.settings import Settings
from railbench.ledger.app import create_app as create_ledger_app
from railbench.ledger.journal import Journal
from railbench.ledger.model import AccountType, NewAccount
from railbench.ledger.tables import accounts, entries, transactions
from railbench.marketplace.service import Marketplace
from railbench.payments.service import Payments
from railbench.scenarios.servers import free_port, serving
from railbench.simulator.app import create_app as create_simulator_app
from railbench.simulator.model import PaymentPlan
from railbench.simulator.provider import Simulator
from tests.integration.helpers import END_OF_TIME, unique

pytestmark = pytest.mark.integration

SECRET = "payouts-secret"
THURSDAY_3PM = datetime(2026, 9, 3, 19, 0, tzinfo=UTC)  # 15:00 New York, before Cutoff
THURSDAY_5PM = datetime(2026, 9, 3, 21, 0, tzinfo=UTC)  # the auction closes
FRIDAY_SETTLEMENT = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)  # 08:30 New York
WEDNESDAY_START = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)  # midnight New York: Available
THURSDAY_AFTER_SETTLEMENT = datetime(2026, 9, 10, 12, 30, tzinfo=UTC)
LIMIT = 50_000
FEE_BPS = 1_000  # 10%, a fictional fee


@dataclass
class Stack:
    clock: ManualClock
    ledger: httpx.Client
    simulator: Simulator
    payments: Payments
    marketplace: Marketplace
    journal: Journal
    engine: Engine

    def tick(self) -> None:
        """What both running services do on their timers."""
        self.simulator.tick()
        self.payments.make_available()
        self.marketplace.release_payouts()

    def at(self, moment: datetime) -> None:
        self.clock.set(moment)
        self.tick()

    def balances(self, code: str) -> dict[str, int]:
        found = self.journal.balances(code, END_OF_TIME)
        return {"posted": found.posted, "pending": found.pending, "available": found.available}

    def customer(self) -> str:
        code = unique("customer")
        self.journal.open_account(NewAccount(code=code, type=AccountType.LIABILITY, currency="USD"))
        return code

    def fund(self, customer: str, amount: int) -> int:
        """An ACH pull into the Customer Balance; returns the Ledger's Payment id."""
        response = self.ledger.post(
            "/payments",
            json={
                "idempotency_key": unique("funding"),
                "rail": "ach",
                "direction": "pull",
                "customer_account": customer,
                "amount": amount,
                "currency": "USD",
            },
        )
        assert response.status_code == 201, response.text
        payment_id: int = response.json()["id"]
        return payment_id

    def close(self, lots: list[tuple[str, str, int]], **extra: Any) -> dict[str, Any]:
        response = self.ledger.post("/auction-closes", json=auction(lots, **extra))
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    def auction_close(self, auction_id: str) -> dict[str, Any]:
        body: dict[str, Any] = self.ledger.get(f"/auction-closes/{auction_id}").json()
        return body

    def payout(self, auction_id: str, seller: str) -> dict[str, Any]:
        found = self.auction_close(auction_id)["payouts"]
        return next(p for p in found if p["customer_account"] == seller)


def auction(lots: list[tuple[str, str, int]], **extra: Any) -> dict[str, Any]:
    return {
        "auction_id": unique("auction"),
        "closed_at": THURSDAY_5PM.isoformat(),
        "currency": "USD",
        "lots": [
            {"lot_id": f"lot-{n}", "buyer": buyer, "seller": seller, "hammer_price": price}
            for n, (buyer, seller, price) in enumerate(lots, start=1)
        ],
        **extra,
    }


@pytest.fixture
def stack(engine: Engine, app_engine: Engine) -> Iterator[Stack]:
    clock = ManualClock(THURSDAY_3PM)
    ledger_port, simulator_port = free_port(), free_port()
    ledger_url = f"http://127.0.0.1:{ledger_port}"
    settings = Settings(
        simulator_url=f"http://127.0.0.1:{simulator_port}",
        ledger_webhook_url=f"{ledger_url}/providers/simulator/events",
        webhook_signing_secret=SECRET,
        instant_payout_limit_cents=LIMIT,
        seller_fee_bps=FEE_BPS,
    )
    ledger_app = create_ledger_app(app_engine, clock, settings)
    simulator_app = create_simulator_app(engine, clock, settings)
    with (
        serving(simulator_app, simulator_port),
        serving(ledger_app, ledger_port),
        httpx.Client(base_url=ledger_url) as ledger,
    ):
        yield Stack(
            clock=clock,
            ledger=ledger,
            simulator=simulator_app.state.simulator,
            payments=ledger_app.state.payments,
            marketplace=ledger_app.state.marketplace,
            journal=ledger_app.state.journal,
            engine=engine,
        )


# Booking the Auction Close


def test_an_auction_close_books_what_each_buyer_owes_and_each_seller_is_owed(
    stack: Stack,
) -> None:
    """Gross, lot by lot: a Customer who both bought and sold owes their purchases and is
    owed their sales (less the fee), each on its own lot's Transaction."""
    avery, bram, cleo = stack.customer(), stack.customer(), stack.customer()
    stack.clock.set(THURSDAY_5PM)

    closed = stack.close(
        [
            (avery, bram, 25_000),  # Avery buys from Bram
            (avery, cleo, 9_999),  # and from Cleo
            (bram, cleo, 4_000),  # Bram, a seller, buys from Cleo too
        ]
    )

    lots = {lot["lot_id"]: lot for lot in closed["lots"]}
    assert [(lot["hammer_price"], lot["fee"]) for lot in lots.values()] == [
        (25_000, 2_500),
        (9_999, 999),  # 10% rounds down, in the seller's favour
        (4_000, 400),
    ]
    customers = {c["customer_account"]: c for c in closed["customers"]}
    assert customers[avery] == {
        "customer_account": avery,
        "owes": 34_999,
        "sold": 0,
        "fees": 0,
        "owed": 0,
    }
    assert customers[bram] == {
        "customer_account": bram,
        "owes": 4_000,
        "sold": 25_000,
        "fees": 2_500,
        "owed": 22_500,
    }
    assert customers[cleo]["owed"] == 9_000 + 3_600
    # Balances in each Account's normal direction: a buyer who hasn't funded owes Mintwharf.
    assert stack.balances(avery)["posted"] == -34_999
    assert stack.balances(bram)["posted"] == 22_500 - 4_000
    assert stack.balances(cleo)["posted"] == 12_600
    fees = [
        -entry.amount
        for lot in lots.values()
        for entry in stack.journal.transaction(lot["transaction_id"]).entries
        if entry.account == "fee-revenue:usd"
    ]
    assert sum(fees) == 3_899  # Fee Revenue, a revenue Account, credited
    # Each lot is one balanced Transaction, by the system job, at the close.
    transaction = stack.journal.transaction(lots["lot-3"]["transaction_id"])
    assert sorted((e.account, e.amount) for e in transaction.entries) == sorted(
        [(bram, 4_000), (cleo, -3_600), ("fee-revenue:usd", -400)]
    )
    assert {e.actor.model_dump()["kind"] for e in transaction.entries} == {"system_job"}
    assert {e.effective_at for e in transaction.entries} == {THURSDAY_5PM}
    # Every seller gets a Payout of what they're owed.
    assert sorted((p["customer_account"], p["amount"]) for p in closed["payouts"]) == sorted(
        [(bram, 22_500), (cleo, 12_600)]
    )


def test_an_auction_close_is_recorded_once_per_auction(stack: Stack) -> None:
    buyer, seller = stack.customer(), stack.customer()
    stack.clock.set(THURSDAY_5PM)
    request = auction([(buyer, seller, 10_000)])

    first = stack.ledger.post("/auction-closes", json=request)
    again = stack.ledger.post("/auction-closes", json=request)
    changed = stack.ledger.post(
        "/auction-closes", json={**request, "lots": [{**request["lots"][0], "hammer_price": 1}]}
    )

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json() == first.json()
    assert changed.status_code == 409
    assert stack.balances(seller)["posted"] == 9_000  # booked once
    assert stack.ledger.get(f"/auction-closes/{request['auction_id']}").json() == first.json()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"lots": []}, id="no lots"),
        pytest.param({"lots": [{"lot_id": "x", "hammer_price": 100}]}, id="a lot with no buyer"),
        pytest.param({"same_customer": True}, id="a Customer buying their own lot"),
        pytest.param({"hammer_price": 12.5}, id="a price that isn't an integer"),
        pytest.param({"unknown_buyer": True}, id="a buyer with no Customer Balance"),
        pytest.param({"instant_for_buyer": True}, id="an Instant Payout for a non-seller"),
        pytest.param({"closed_at": "2026-09-04T00:00:00+00:00"}, id="a close in the future"),
    ],
)
def test_an_auction_close_the_ledger_cannot_book_is_refused(
    stack: Stack, change: dict[str, Any]
) -> None:
    buyer, seller = stack.customer(), stack.customer()
    stack.clock.set(THURSDAY_5PM)
    lot: dict[str, Any] = {"lot_id": "lot-1", "buyer": buyer, "seller": seller}
    lot["hammer_price"] = change.pop("hammer_price", 1_000)
    if change.pop("same_customer", False):
        lot["buyer"] = seller
    if change.pop("unknown_buyer", False):
        lot["buyer"] = unique("nobody")
    request = {**auction([]), "lots": [lot]}
    if change.pop("instant_for_buyer", False):
        request["instant_payouts"] = [buyer]
    request.update(change)

    response = stack.ledger.post("/auction-closes", json=request)

    assert response.status_code == 422, response.text
    assert stack.balances(seller)["posted"] == 0
    assert stack.ledger.get(f"/auction-closes/{request['auction_id']}").status_code == 404


# Standard Payouts


def test_a_standard_payout_waits_until_the_buyers_funds_are_available(stack: Stack) -> None:
    buyer, seller = stack.customer(), stack.customer()
    funding = stack.fund(buyer, 25_000)
    stack.tick()  # submitted
    stack.clock.set(THURSDAY_5PM)
    closed = stack.close([(buyer, seller, 25_000)])
    auction_id = closed["auction_id"]

    payout = stack.payout(auction_id, seller)
    assert (payout["requested"], payout["speed"], payout["amount"]) == (
        "standard",
        "standard",
        22_500,
    )
    assert [s["step"] for s in payout["steps"]] == ["held"]
    # The seller's proceeds are Posted but held: not Available until the buyer's are.
    assert stack.balances(seller) == {"posted": 22_500, "pending": 0, "available": 0}

    # The buyer's pull settles Friday, but its funds aren't Available over the weekend,
    # Labor Day or Tuesday, so neither is the Payout.
    for moment in (
        FRIDAY_SETTLEMENT,
        datetime(2026, 9, 7, 16, 0, tzinfo=UTC),
        WEDNESDAY_START - timedelta(seconds=1),
    ):
        stack.at(moment)
        assert [s["step"] for s in stack.payout(auction_id, seller)["steps"]] == ["held"]
        assert stack.payout(auction_id, seller)["payments"] == []
    assert stack.balances(buyer)["available"] < 0

    # Wednesday at midnight New York: the buyer's funds are Available, and so the Payout is
    # released and sent over ACH.
    stack.at(WEDNESDAY_START)
    assert stack.payments.payment(funding).state == "available"
    payout = stack.payout(auction_id, seller)
    assert [s["step"] for s in payout["steps"]] == ["held", "released"]
    assert payout["steps"][1]["effective_at"] == "2026-09-09T04:00:00Z"
    [sent] = payout["payments"]
    assert sent["rail"] == "ach"
    stack.tick()  # the Simulator reports it submitted
    assert stack.payments.payment(sent["payment_id"]).state == "submitted"
    assert stack.balances(seller) == {"posted": 22_500, "pending": -22_500, "available": 0}

    # The ACH push settles on the next Banking Day: the seller is paid.
    stack.at(THURSDAY_AFTER_SETTLEMENT)
    assert stack.payments.payment(sent["payment_id"]).state == "settled"
    assert stack.balances(seller) == {"posted": 0, "pending": 0, "available": 0}
    assert stack.balances(buyer) == {"posted": 0, "pending": 0, "available": 0}
    assert clearing_net(stack, sent["payment_id"]) == 0


def clearing_net(stack: Stack, payment_id: int) -> int:
    """What one Payment's own Transactions add up to on Clearing Accounts (which other tests
    share): every Ledger row a Payment books is keyed `payment:<id>:<step>`."""
    with stack.engine.connect() as connection:
        total = connection.scalar(
            select(func.coalesce(func.sum(entries.c.amount), 0))
            .join(transactions, transactions.c.id == entries.c.transaction_id)
            .join(accounts, accounts.c.id == entries.c.account_id)
            .where(
                transactions.c.idempotency_key.startswith(f"payment:{payment_id}:"),
                accounts.c.code.startswith("clearing:"),
            )
        )
    return int(total or 0)


def test_a_payout_waits_for_every_buyer_behind_it(stack: Stack) -> None:
    """Two buyers, one funded and one not: the seller's Payout waits for both."""
    funded, unfunded, seller = stack.customer(), stack.customer(), stack.customer()
    stack.fund(funded, 5_000)
    stack.tick()
    stack.clock.set(THURSDAY_5PM)
    closed = stack.close([(funded, seller, 5_000), (unfunded, seller, 1_000)])

    stack.at(FRIDAY_SETTLEMENT)
    stack.at(WEDNESDAY_START)

    assert [s["step"] for s in stack.payout(closed["auction_id"], seller)["steps"]] == ["held"]


def test_a_buyer_who_also_sold_must_fund_their_own_purchases(stack: Stack) -> None:
    """Gross, not net: Bram's sale proceeds are held for his own Payout, so they don't count
    as Available money paying for his purchase, and Cleo, who sold to him, waits for his
    funding, not for the buyers behind his proceeds."""
    avery, bram, cleo = stack.customer(), stack.customer(), stack.customer()
    stack.fund(bram, 4_000)
    stack.tick()
    stack.clock.set(THURSDAY_5PM)
    closed = stack.close([(avery, bram, 25_000), (bram, cleo, 4_000)])
    auction_id = closed["auction_id"]

    stack.at(FRIDAY_SETTLEMENT)
    stack.at(WEDNESDAY_START)  # Bram's funding is Available; Avery never funded

    assert [s["step"] for s in stack.payout(auction_id, cleo)["steps"]] == ["held", "released"]
    assert [s["step"] for s in stack.payout(auction_id, bram)["steps"]] == ["held"]


# Instant Payouts


def test_an_instant_payout_goes_out_over_rtp_at_once_at_mintwharfs_risk(stack: Stack) -> None:
    buyer, seller = stack.customer(), stack.customer()
    stack.fund(buyer, 30_000)
    stack.tick()
    stack.clock.set(THURSDAY_5PM)

    closed = stack.close([(buyer, seller, 30_000)], instant_payouts=[seller])

    payout = stack.payout(closed["auction_id"], seller)
    assert (payout["requested"], payout["speed"], payout["amount"]) == (
        "instant",
        "instant",
        27_000,
    )
    assert [s["step"] for s in payout["steps"]] == ["instant"]
    [sent] = payout["payments"]
    assert sent["rail"] == "rtp"
    # The buyer's pull hasn't even settled, and the seller is paid on the next tick.
    stack.tick()
    assert stack.payments.payment(sent["payment_id"]).state == "settled"
    assert stack.balances(seller) == {"posted": 0, "pending": 0, "available": 0}
    assert stack.balances(buyer) == {"posted": -30_000, "pending": 30_000, "available": -30_000}
    assert clearing_net(stack, sent["payment_id"]) == 0
    # Nothing more for the release job to do with it, ever.
    stack.at(WEDNESDAY_START)
    assert [s["step"] for s in stack.payout(closed["auction_id"], seller)["steps"]] == ["instant"]


@pytest.mark.parametrize(
    ("hammer_price", "owed", "speed"),
    [
        pytest.param(55_555, LIMIT, "instant", id="at the limit: within it"),
        pytest.param(55_556, LIMIT + 1, "standard", id="a cent over: falls back to Standard"),
    ],
)
def test_an_instant_payout_over_the_limit_falls_back_to_a_standard_payout(
    stack: Stack, hammer_price: int, owed: int, speed: str
) -> None:
    buyer, seller = stack.customer(), stack.customer()
    stack.clock.set(THURSDAY_5PM)

    closed = stack.close([(buyer, seller, hammer_price)], instant_payouts=[seller])

    payout = stack.payout(closed["auction_id"], seller)
    assert (payout["amount"], payout["requested"], payout["speed"]) == (owed, "instant", speed)
    if speed == "standard":
        assert [s["step"] for s in payout["steps"]] == ["held"]
        assert "over the limit" in payout["steps"][0]["detail"]
        assert payout["payments"] == []
        assert stack.balances(seller)["available"] == 0  # held like any Standard Payout
    else:
        assert [p["rail"] for p in payout["payments"]] == ["rtp"]


def test_the_limit_counts_instant_payouts_whose_funds_are_not_available_yet(
    stack: Stack,
) -> None:
    seller = stack.customer()
    first_buyer, second_buyer, third_buyer = stack.customer(), stack.customer(), stack.customer()
    stack.fund(first_buyer, 30_000)
    stack.tick()
    stack.clock.set(THURSDAY_5PM)
    first = stack.close([(first_buyer, seller, 30_000)], instant_payouts=[seller])
    # 27,000 is out at risk, so another 27,000 would take the exposure over 50,000.
    second = stack.close([(second_buyer, seller, 30_000)], instant_payouts=[seller])

    assert stack.payout(first["auction_id"], seller)["speed"] == "instant"
    assert stack.payout(second["auction_id"], seller)["speed"] == "standard"

    # Once the first buyer's funds are Available, that risk is over and the limit frees up.
    stack.at(FRIDAY_SETTLEMENT)
    stack.at(WEDNESDAY_START)
    third = stack.close([(third_buyer, seller, 30_000)], instant_payouts=[seller])
    assert stack.payout(third["auction_id"], seller)["speed"] == "instant"


def test_a_declined_instant_payout_falls_back_to_a_standard_payout(stack: Stack) -> None:
    """RTP declined it, so nothing left the Customer Balance: Mintwharf still owes the
    seller, and pays them the Standard way once the buyer's funds are Available."""
    buyer, seller = stack.customer(), stack.customer()
    stack.fund(buyer, 30_000)
    stack.tick()
    stack.clock.set(THURSDAY_5PM)
    request = auction([(buyer, seller, 30_000)], instant_payouts=[seller])
    auction_id = request["auction_id"]
    stack.simulator.plan(
        PaymentPlan(
            idempotency_key=f"auction:{auction_id}:payout:{seller}:rtp",
            rail="rtp",
            outcome="decline",
        )
    )
    assert stack.ledger.post("/auction-closes", json=request).status_code == 201

    stack.tick()  # the decline arrives, and the release job falls back at once
    payout = stack.payout(auction_id, seller)
    assert (payout["requested"], payout["speed"]) == ("instant", "standard")
    assert [s["step"] for s in payout["steps"]] == ["instant", "held"]
    assert "declined" in payout["steps"][1]["detail"]
    assert [(p["rail"], p["state"]) for p in payout["payments"]] == [("rtp", "declined")]
    assert stack.balances(seller) == {"posted": 27_000, "pending": 0, "available": 0}

    stack.at(FRIDAY_SETTLEMENT)
    stack.at(WEDNESDAY_START)
    payout = stack.payout(auction_id, seller)
    assert [s["step"] for s in payout["steps"]] == ["instant", "held", "released"]
    assert [p["rail"] for p in payout["payments"]] == ["rtp", "ach"]
    stack.tick()
    stack.at(THURSDAY_AFTER_SETTLEMENT)
    assert stack.balances(seller) == {"posted": 0, "pending": 0, "available": 0}
