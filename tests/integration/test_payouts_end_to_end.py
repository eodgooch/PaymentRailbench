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
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier, BrokenBarrierError
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select

from railbench.common.clock import ManualClock
from railbench.common.settings import Settings
from railbench.ledger.app import create_app as create_ledger_app
from railbench.ledger.journal import Journal
from railbench.ledger.model import AccountType, NewAccount, NewEntry, NewTransaction, SystemJob
from railbench.ledger.tables import accounts, entries, transactions
from railbench.marketplace.model import NewAuctionClose
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

    def fund(self, customer: str, amount: int, returned: dict[str, Any] | None = None) -> int:
        """An ACH pull into the Customer Balance, returned as planned if `returned` gives a
        code and `at`; returns the Ledger's Payment id."""
        key = unique("funding")
        if returned is not None:
            self.simulator.plan(
                PaymentPlan.model_validate(
                    {"idempotency_key": key, "faults": [{"id": "r", "kind": "return", **returned}]}
                )
            )
        response = self.ledger.post(
            "/payments",
            json={
                "idempotency_key": key,
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
    assert [s["step"] for s in payout["steps"]] == ["reserved", "standard"]
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
        assert [s["step"] for s in stack.payout(auction_id, seller)["steps"]] == [
            "reserved",
            "standard",
        ]
        assert stack.payout(auction_id, seller)["payments"] == []
    assert stack.balances(buyer)["available"] < 0

    # Wednesday at midnight New York: the buyer's funds are Available, and so the Payout is
    # released and sent over ACH.
    stack.at(WEDNESDAY_START)
    assert stack.payments.payment(funding).state == "available"
    payout = stack.payout(auction_id, seller)
    assert [s["step"] for s in payout["steps"]] == ["reserved", "standard", "released"]
    assert payout["steps"][2]["effective_at"] == "2026-09-09T04:00:00Z"
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

    assert [s["step"] for s in stack.payout(closed["auction_id"], seller)["steps"]] == [
        "reserved",
        "standard",
    ]
    assert stack.payout(closed["auction_id"], seller)["waiting_for"] == [unfunded]


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

    assert [s["step"] for s in stack.payout(auction_id, cleo)["steps"]] == [
        "reserved",
        "standard",
        "released",
    ]
    assert [s["step"] for s in stack.payout(auction_id, bram)["steps"]] == ["reserved", "standard"]


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
    assert [s["step"] for s in payout["steps"]] == ["reserved", "instant"]
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
    assert [s["step"] for s in stack.payout(closed["auction_id"], seller)["steps"]] == [
        "reserved",
        "instant",
    ]


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
        assert [s["step"] for s in payout["steps"]] == ["reserved", "standard"]
        assert "over the limit" in payout["steps"][1]["detail"]
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
    assert [s["step"] for s in payout["steps"]] == ["reserved", "instant", "standard"]
    assert "declined" in payout["steps"][2]["detail"]
    assert [(p["rail"], p["state"]) for p in payout["payments"]] == [("rtp", "declined")]
    assert stack.balances(seller) == {"posted": 27_000, "pending": 0, "available": 0}

    stack.at(FRIDAY_SETTLEMENT)
    stack.at(WEDNESDAY_START)
    payout = stack.payout(auction_id, seller)
    assert [s["step"] for s in payout["steps"]] == ["reserved", "instant", "standard", "released"]
    assert [p["rail"] for p in payout["payments"]] == ["rtp", "ach"]
    stack.tick()
    stack.at(THURSDAY_AFTER_SETTLEMENT)
    assert stack.balances(seller) == {"posted": 0, "pending": 0, "available": 0}


# Proceeds on their way out are never Available (ADR 0012)


def fund_now(stack: Stack, customer: str, amount: int) -> None:
    """Money already Available in the Customer Balance, without an ACH pull."""
    source = unique("test-funding")
    stack.journal.open_account(NewAccount(code=source, type=AccountType.ASSET, currency="USD"))
    stack.journal.post(
        NewTransaction(
            idempotency_key=unique("fund-now"),
            actor=SystemJob(name="test-funding", version="1"),
            effective_at=THURSDAY_3PM,
            entries=[
                NewEntry(account=source, currency="USD", amount=amount),
                NewEntry(account=customer, currency="USD", amount=-amount),
            ],
        )
    )


def steps(stack: Stack, auction_id: str, seller: str) -> list[str]:
    return [s["step"] for s in stack.payout(auction_id, seller)["steps"]]


def reservation(stack: Stack, auction_id: str, seller: str) -> Any:
    [hold_id] = [s["hold_id"] for s in stack.payout(auction_id, seller)["steps"] if s["hold_id"]]
    return stack.journal.hold(hold_id)


@pytest.fixture
def bram_sells_then_buys(stack: Stack) -> tuple[str, str, str]:
    """Zoe (funded) buys Bram's lot; Bram is owed 22,500 and will buy Yuki's lot for 4,000
    without ever funding it. Returns (bram, yuki, zoe)."""
    bram, yuki, zoe = stack.customer(), stack.customer(), stack.customer()
    fund_now(stack, zoe, 25_000)
    stack.clock.set(THURSDAY_5PM)
    return bram, yuki, zoe


def test_an_instant_payout_not_yet_booked_does_not_fund_its_sellers_purchases(
    stack: Stack, bram_sells_then_buys: tuple[str, str, str]
) -> None:
    """(a) Bram's Instant Payout is decided and sent, but RTP hasn't reported it settled."""
    bram, yuki, zoe = bram_sells_then_buys
    sold = stack.close([(zoe, bram, 25_000)], instant_payouts=[bram])
    assert stack.payout(sold["auction_id"], bram)["speed"] == "instant"
    assert stack.balances(bram) == {"posted": 22_500, "pending": 0, "available": 0}

    bought = stack.close([(bram, yuki, 4_000)])
    stack.marketplace.release_payouts()  # no Provider Event yet: the RTP isn't booked

    assert stack.balances(bram)["available"] == -4_000
    assert steps(stack, bought["auction_id"], yuki) == ["reserved", "standard"]
    assert stack.payout(bought["auction_id"], yuki)["waiting_for"] == [bram]

    stack.tick()  # RTP settles: the Transaction paying Bram captures the reservation
    assert reservation(stack, sold["auction_id"], bram).ended.outcome == "captured"
    assert stack.balances(bram) == {"posted": -4_000, "pending": 0, "available": -4_000}
    assert steps(stack, bought["auction_id"], yuki) == ["reserved", "standard"]


def test_a_declined_instant_payout_does_not_fund_its_sellers_purchases(
    stack: Stack, bram_sells_then_buys: tuple[str, str, str]
) -> None:
    """(b) RTP declined Bram's Instant Payout; the release job hasn't run since."""
    bram, yuki, zoe = bram_sells_then_buys
    request = auction([(zoe, bram, 25_000)], instant_payouts=[bram])
    stack.simulator.plan(
        PaymentPlan(
            idempotency_key=f"auction:{request['auction_id']}:payout:{bram}:rtp",
            rail="rtp",
            outcome="decline",
        )
    )
    assert stack.ledger.post("/auction-closes", json=request).status_code == 201
    bought = stack.close([(bram, yuki, 4_000)])

    stack.simulator.tick()  # the decline is applied; the release job hasn't run
    assert stack.balances(bram)["available"] == -4_000  # the reservation stays

    stack.marketplace.release_payouts()
    assert steps(stack, request["auction_id"], bram) == ["reserved", "instant", "standard"]
    assert steps(stack, bought["auction_id"], yuki) == ["reserved", "standard"]
    # Bram now waits on himself: he owes for Yuki's lot.
    assert stack.payout(request["auction_id"], bram)["waiting_for"] == [bram]


def test_a_released_payout_not_yet_submitted_does_not_fund_its_sellers_purchases(
    stack: Stack, bram_sells_then_buys: tuple[str, str, str]
) -> None:
    """(c) Bram's Standard Payout is released and its ACH push created, but the Provider
    hasn't reported it submitted (and might not for a long time)."""
    bram, yuki, zoe = bram_sells_then_buys
    sold = stack.close([(zoe, bram, 25_000)])
    stack.marketplace.release_payouts()  # Zoe's money is Available: released
    assert steps(stack, sold["auction_id"], bram) == ["reserved", "standard", "released"]

    bought = stack.close([(bram, yuki, 4_000)])
    stack.marketplace.release_payouts()

    assert stack.balances(bram)["available"] == -4_000
    assert steps(stack, bought["auction_id"], yuki) == ["reserved", "standard"]
    stack.tick()  # submitted: the pending outflow captures the reservation
    assert reservation(stack, sold["auction_id"], bram).ended.outcome == "captured"
    assert stack.balances(bram) == {"posted": 18_500, "pending": -22_500, "available": -4_000}
    assert steps(stack, bought["auction_id"], yuki) == ["reserved", "standard"]


@pytest.mark.parametrize("speed", ["standard", "instant"])
def test_a_seller_who_owes_for_their_own_purchases_is_not_paid_out(
    stack: Stack, speed: str
) -> None:
    """Bram buys a lot he never funds and sells one to a buyer who has: he isn't paid until
    he's paid for his own, Instant Payout or not."""
    bram, yuki, zoe = stack.customer(), stack.customer(), stack.customer()
    fund_now(stack, zoe, 25_000)
    stack.clock.set(THURSDAY_5PM)
    instant = [bram] if speed == "instant" else []

    closed = stack.close([(zoe, bram, 25_000), (bram, yuki, 4_000)], instant_payouts=instant)
    stack.at(WEDNESDAY_START)

    payout = stack.payout(closed["auction_id"], bram)
    assert payout["speed"] == "standard"
    if speed == "instant":
        assert "owe" in payout["steps"][1]["detail"]
    assert payout["payments"] == []
    assert payout["waiting_for"] == [bram]
    assert stack.balances(bram) == {"posted": 18_500, "pending": 0, "available": -4_000}

    fund_now(stack, bram, 4_000)  # he pays for Yuki's lot: both are paid out
    stack.tick()
    assert "released" in steps(stack, closed["auction_id"], bram)
    assert "released" in steps(stack, closed["auction_id"], yuki)


@pytest.mark.parametrize(
    ("faults", "detail"),
    [
        pytest.param((), "reversed", id="declined after submission: the pending outflow reversed"),
        pytest.param(
            ({"id": "swap", "kind": "out_of_order", "event": "submitted", "after": "declined"},),
            "reservation released",
            id="declined before the Ledger heard it submitted: the reservation released",
        ),
    ],
)
def test_a_declined_standard_payout_leaves_the_money_with_the_seller(
    stack: Stack, faults: tuple[dict[str, Any], ...], detail: str
) -> None:
    seller, buyer = stack.customer(), stack.customer()
    fund_now(stack, buyer, 25_000)
    stack.clock.set(THURSDAY_5PM)
    request = auction([(buyer, seller, 25_000)])
    stack.simulator.plan(
        PaymentPlan.model_validate(
            {
                "idempotency_key": f"auction:{request['auction_id']}:payout:{seller}:ach",
                "outcome": "decline",
                "faults": list(faults),
            }
        )
    )
    assert stack.ledger.post("/auction-closes", json=request).status_code == 201
    stack.tick()  # released and sent

    # Sent after Thursday's Cutoff, the ACH push counts toward Friday and would settle on
    # Tuesday, after Labor Day: it's declined then instead.
    stack.at(datetime(2026, 9, 8, 12, 30, tzinfo=UTC))
    stack.tick()

    payout = stack.payout(request["auction_id"], seller)
    assert [s["step"] for s in payout["steps"]] == ["reserved", "standard", "released", "returned"]
    assert detail in payout["steps"][-1]["detail"]
    assert stack.balances(seller) == {"posted": 22_500, "pending": 0, "available": 22_500}


def test_two_auctions_closing_at_once_cannot_both_spend_a_sellers_limit(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each Instant Payout of 27,000 fits the 50,000 limit alone, but not both: whichever
    auction closes second sees the first's exposure, however close together they are."""
    seller = stack.customer()
    buyers = [stack.customer(), stack.customer()]
    stack.clock.set(THURSDAY_5PM)
    barrier = Barrier(2)
    measure = Marketplace._instant_exposure

    def slowly(self: Marketplace, connection: Any, customer: str) -> int:
        found = measure(self, connection, customer)
        with suppress(BrokenBarrierError):  # the other close is waiting on the seller's lock
            barrier.wait(timeout=1)
        return found

    monkeypatch.setattr(Marketplace, "_instant_exposure", slowly)
    requests = [
        NewAuctionClose.model_validate(auction([(buyer, seller, 30_000)], instant_payouts=[seller]))
        for buyer in buyers
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        closed = list(pool.map(stack.marketplace.close, requests))

    speeds = sorted(
        next(p["speed"] for p in c.auction_close.model_dump()["payouts"]) for c in closed
    )
    assert speeds == ["instant", "standard"]


# Returns (ADR 0013): a buyer's Return takes their funding back, so the Payouts it was behind
# are at risk again; the deficit in Recovery Receivable keeps them so.
LABOR_DAY = datetime(2026, 9, 7, 16, 0, tzinfo=UTC)


def test_a_standard_payout_behind_a_returned_buyer_goes_on_waiting(stack: Stack) -> None:
    buyer, seller = stack.customer(), stack.customer()
    funding = stack.fund(buyer, 25_000, {"code": "R01", "at": LABOR_DAY - FRIDAY_SETTLEMENT})
    stack.tick()
    stack.clock.set(THURSDAY_5PM)
    closed = stack.close([(buyer, seller, 25_000)])

    stack.at(FRIDAY_SETTLEMENT)
    stack.at(LABOR_DAY)  # R01: insufficient funds, before the funds were Available
    stack.at(WEDNESDAY_START)
    stack.at(THURSDAY_AFTER_SETTLEMENT)

    assert stack.payments.payment(funding).state == "returned"
    # The buyer's balance is back to zero, the deficit in Recovery Receivable: they still
    # owe for the lot, so the seller's Payout waits for them.
    assert stack.balances(buyer) == {"posted": 0, "pending": 0, "available": 0}
    assert stack.balances(f"recovery-receivable:{buyer}")["posted"] == 25_000
    payout = stack.payout(closed["auction_id"], seller)
    assert [s["step"] for s in payout["steps"]] == ["reserved", "standard"]
    assert payout["waiting_for"] == [buyer]

    # Once the buyer funds their purchase again, the Payout goes.
    stack.fund(buyer, 25_000)
    stack.tick()
    stack.at(datetime(2026, 9, 11, 12, 30, tzinfo=UTC))  # Friday: settles
    stack.at(datetime(2026, 9, 15, 4, 0, tzinfo=UTC))  # Tuesday: Available
    payout = stack.payout(closed["auction_id"], seller)
    assert [s["step"] for s in payout["steps"]] == ["reserved", "standard", "released"]


def test_a_late_return_puts_an_instant_payouts_risk_back_on_the_limit(stack: Stack) -> None:
    seller, first_buyer, second_buyer = stack.customer(), stack.customer(), stack.customer()
    stack.fund(first_buyer, 30_000, {"code": "R10", "at": "P10D"})
    stack.tick()
    stack.clock.set(THURSDAY_5PM)
    first = stack.close([(first_buyer, seller, 30_000)], instant_payouts=[seller])
    assert stack.payout(first["auction_id"], seller)["speed"] == "instant"
    stack.at(FRIDAY_SETTLEMENT)
    stack.at(WEDNESDAY_START)  # Available: the risk looked over

    stack.at(FRIDAY_SETTLEMENT + timedelta(days=10))  # the Late Return
    second = stack.close(
        [(second_buyer, seller, 30_000)],
        instant_payouts=[seller],
        closed_at=stack.clock.now().isoformat(),
    )

    # 27,000 is at risk again (the first buyer owes it to Mintwharf), so another 27,000
    # would take the seller over the limit.
    assert stack.balances(f"recovery-receivable:{first_buyer}")["posted"] == 30_000
    assert stack.payout(second["auction_id"], seller)["speed"] == "standard"
    assert stack.balances(seller)["posted"] == 27_000  # the first Payout stays paid
