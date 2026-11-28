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
    assert found.payments
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
