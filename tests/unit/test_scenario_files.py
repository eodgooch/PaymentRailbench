"""Scenario files (SPEC §6, ADR 0011): YAML parsed into strict models, and resolved with a
seed into the concrete plans the Simulator follows. Same file and seed, same plans."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from railbench.scenarios.model import Scenario, load
from railbench.simulator.model import DelayedDelivery, DuplicateDelivery, OutOfOrderDelivery

SCENARIOS = Path(__file__).parents[2] / "scenarios"

BASE: dict[str, Any] = {
    "name": "two-pulls",
    "seed": 7,
    "start": "2026-09-03T15:00:00-04:00",
    "customers": [{"id": "c-avery", "name": "Avery Quill"}],
    "payments": [
        {"id": "first", "customer": "c-avery", "amount": 25_000},
        {"id": "second", "customer": "c-avery", "amount": 1_250, "outcome": "decline"},
    ],
}


def scenario(**changes: Any) -> Scenario:
    return Scenario.model_validate({**BASE, **changes})


@pytest.mark.parametrize("path", sorted(SCENARIOS.glob("*.yaml")), ids=lambda p: p.stem)
def test_every_committed_scenario_file_parses(path: Path) -> None:
    found = load(path)

    assert found.name == path.stem
    assert found.payments or found.auction_closes
    for seed in (found.seed, found.seed + 1):
        assert found.plans(seed)  # every Fault fits its Payment's outcome


def test_a_scenario_reads_from_yaml() -> None:
    found = Scenario.model_validate(yaml.safe_load(yaml.safe_dump(BASE)))

    assert found.start == datetime(2026, 9, 3, 19, 0, tzinfo=UTC)
    assert [(p.id, p.amount, p.outcome) for p in found.payments] == [
        ("first", 25_000, "settle"),
        ("second", 1_250, "decline"),
    ]


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"start": "2026-09-03T15:00:00"}, id="start without a UTC offset"),
        pytest.param({"colour": "blue"}, id="an unknown key"),
        pytest.param(
            {"payments": [{"id": "p", "customer": "c-avery", "amount": 250.0}]},
            id="an amount that isn't an integer",
        ),
        pytest.param(
            {"payments": [{"id": "p", "customer": "c-avery", "amount": "250"}]},
            id="an amount as a string",
        ),
        pytest.param(
            {"payments": [{"id": "p", "customer": "nobody", "amount": 250}]},
            id="an unknown Customer",
        ),
        pytest.param(
            {"payments": [BASE["payments"][0], BASE["payments"][0]]}, id="a repeated Payment id"
        ),
        pytest.param(
            {
                "faults": [
                    {"id": "f", "kind": "duplicate", "payment": "nobody", "event": "submitted"}
                ]
            },
            id="a Fault on an unknown Payment",
        ),
        pytest.param(
            {"faults": [{"id": "f", "kind": "duplicate", "payment": "second", "event": "settled"}]},
            id="a Fault on an event the Payment never emits",
        ),
        pytest.param(
            {"faults": [{"id": "f", "kind": "reorg", "payment": "first", "event": "settled"}]},
            id="an unknown Fault kind",
        ),
        pytest.param({"seed": "7"}, id="a seed that isn't an integer"),
    ],
)
def test_a_malformed_scenario_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        scenario(**changes)


def test_named_faults_become_the_simulators_plans() -> None:
    found = scenario(
        faults=[
            {"id": "dup", "kind": "duplicate", "payment": "first", "event": "settled", "times": 2},
            {
                "id": "late",
                "kind": "delay",
                "payment": "second",
                "event": "submitted",
                "by": "PT3H",
            },
            {
                "id": "swap",
                "kind": "out_of_order",
                "payment": "second",
                "event": "submitted",
                "after": "declined",
            },
        ]
    )

    plans = found.plans(7)

    assert plans["first"].idempotency_key == "scenario:two-pulls:7:first"
    assert plans["first"].faults == (DuplicateDelivery(id="dup", event="settled", times=2),)
    assert plans["second"].outcome == "decline"
    assert plans["second"].faults == (
        DelayedDelivery(id="late", event="submitted", by=timedelta(hours=3)),
        OutOfOrderDelivery(id="swap", event="submitted", after="declined"),
    )


def random_scenario() -> Scenario:
    return scenario(
        payments=[{"id": f"p{n}", "customer": "c-avery", "amount": 1_000 + n} for n in range(12)],
        random_faults={"rate": 0.5, "kinds": ["duplicate", "delay", "out_of_order"]},
    )


def test_random_faults_are_the_same_for_the_same_seed_and_differ_for_another() -> None:
    found = random_scenario()

    assert found.plans(7) == found.plans(7)
    assert found.plans(7) != found.plans(8)
    faults = [f for plan in found.plans(7).values() for f in plan.faults]
    assert 0 < len(faults) < 24  # a rate of 0.5 over 24 events
    assert {f.kind for f in faults} == {"duplicate", "delay", "out_of_order"}
    assert all(f.id.startswith("random-") for f in faults)


# Auction Closes in a Scenario (ADR 0012)

AUCTION: dict[str, Any] = {
    "id": "fall-auction",
    "at": "PT2H",
    "lots": [
        {"id": "lot-1", "buyer": "c-avery", "seller": "c-bram", "hammer_price": 25_000},
        {"id": "lot-2", "buyer": "c-bram", "seller": "c-cleo", "hammer_price": 4_000},
    ],
    "payouts": [
        {"id": "cleo-payout", "seller": "c-cleo", "speed": "instant", "rtp_outcome": "decline"},
        {"id": "bram-payout", "seller": "c-bram"},
    ],
}
SELLERS = [
    {"id": "c-avery", "name": "Avery Quill"},
    {"id": "c-bram", "name": "Bram Okonkwo-Hale"},
    {"id": "c-cleo", "name": "Cleo Marchetti"},
]


def with_auction(auction: dict[str, Any] | None = None, **changes: Any) -> Scenario:
    return scenario(customers=SELLERS, auction_closes=[auction or AUCTION], **changes)


def test_an_auction_close_names_its_lots_and_payouts() -> None:
    found = with_auction()

    [close] = found.auction_closes
    assert found.auction_id(close, 7) == "two-pulls:7:fall-auction"
    assert [(lot.buyer, lot.seller, lot.hammer_price) for lot in close.lots] == [
        ("c-avery", "c-bram", 25_000),
        ("c-bram", "c-cleo", 4_000),
    ]
    assert found.instant_payouts(close) == ["customer:c-cleo:usd"]


def test_a_payout_is_planned_on_every_rail_it_can_go_out_on() -> None:
    """An Instant Payout may go out over RTP or, over the limit or declined, over ACH: the
    Simulator gets a plan for each, under the Idempotency Keys the Ledger will use."""
    found = with_auction(
        faults=[
            {"id": "dup", "kind": "duplicate", "payment": "cleo-payout", "event": "declined"},
            {
                "id": "late",
                "kind": "delay",
                "payment": "bram-payout",
                "event": "submitted",
                "by": "PT1H",
            },
        ]
    )

    plans = found.plans(7)

    rtp, ach = plans["cleo-payout:rtp"], plans["cleo-payout:ach"]
    assert rtp.idempotency_key == "auction:two-pulls:7:fall-auction:payout:customer:c-cleo:usd:rtp"
    assert (rtp.rail, rtp.outcome, [f.id for f in rtp.faults]) == ("rtp", "decline", ["dup"])
    assert (ach.rail, ach.outcome, ach.faults) == ("ach", "settle", ())
    assert "bram-payout:rtp" not in plans  # a Standard Payout only ever goes over ACH
    assert [f.id for f in plans["bram-payout:ach"].faults] == ["late"]


@pytest.mark.parametrize(
    "auction",
    [
        pytest.param(
            {
                **AUCTION,
                "lots": [{"id": "l", "buyer": "nobody", "seller": "c-bram", "hammer_price": 1}],
            },
            id="a lot with an unknown buyer",
        ),
        pytest.param(
            {**AUCTION, "payouts": [{"id": "p", "seller": "c-avery", "speed": "instant"}]},
            id="a Payout for a Customer who sold nothing",
        ),
        pytest.param(
            {**AUCTION, "lots": [{**AUCTION["lots"][0], "hammer_price": 10.5}]},
            id="a price that isn't an integer",
        ),
    ],
)
def test_a_malformed_auction_close_is_refused(auction: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        with_auction(auction)


def test_a_fault_on_a_payout_must_hit_an_event_one_of_its_payments_emits() -> None:
    with pytest.raises(ValidationError, match="never emits"):
        with_auction(
            faults=[{"id": "f", "kind": "duplicate", "payment": "bram-payout", "event": "declined"}]
        )
