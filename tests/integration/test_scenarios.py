"""Scenario runs (SPEC §4.2, §6, ADR 0011): a named, seeded Scenario replays the same way
every run, delivery Faults included, and the Ledger stays correct under them. Each run gets
its own freshly migrated database, so a run is the whole Ledger."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select

from railbench.common.db import LEDGER_APP_ROLE, make_engine
from railbench.payments.tables import inbox
from railbench.scenarios.model import Scenario, load
from railbench.scenarios.replay import availability_job_runs, replay_inbox
from railbench.scenarios.runner import Run, fresh_database, run
from railbench.scenarios.summary import ledger_summary

pytestmark = pytest.mark.integration

SCENARIOS = Path(__file__).parents[2] / "scenarios"
THURSDAY_3PM_NEW_YORK = "2026-09-03T15:00:00-04:00"


@pytest.fixture
def fresh(database_url: str) -> Iterator[str]:
    with fresh_database(database_url) as url:
        yield url


@pytest.fixture
def manifests(tmp_path: Path) -> Path:
    return tmp_path


def run_in_fresh_database(
    database_url: str, manifests: Path, scenario: Scenario, seed: int | None = None
) -> Run:
    with fresh_database(database_url) as url:
        return run(scenario, url, seed, manifests / f"{scenario.name}-{seed}.json")


def payment(found: Run, payment_id: str) -> dict[str, Any]:
    key = found.scenario.plans(found.seed)[payment_id].idempotency_key
    summary: dict[str, Any] = found.summary["payments"][key]
    return summary


def states(found: Run, payment_id: str) -> list[str]:
    return [step["state"] for step in payment(found, payment_id)["transitions"]]


def outcomes(found: Run, payment_id: str) -> list[str]:
    return [o["outcome"] for o in payment(found, payment_id)["inbox_outcomes"]]


def no_exceptions(found: Run) -> bool:
    return all(
        o["outcome"] != "exception"
        for p in found.summary["payments"].values()
        for o in p["inbox_outcomes"]
    )


def test_duplicate_webhooks_are_applied_once(database_url: str, manifests: Path) -> None:
    found = run_in_fresh_database(
        database_url, manifests, load(SCENARIOS / "ach-pull-duplicates.yaml")
    )

    assert [(f.fault_id, f.cause_code) for f in found.manifest.faults] == [
        ("dup-avery-submitted", "duplicate_delivery"),
        ("dup-bram-settled", "duplicate_delivery"),
    ]
    for payment_id in ("avery-funding", "bram-funding"):
        assert states(found, payment_id) == ["created", "submitted", "settled", "available"]
        # One Inbox row per event, each applied once, however many times it was delivered.
        assert outcomes(found, payment_id) == ["applied", "applied"]
    assert no_exceptions(found)


def test_out_of_order_and_delayed_webhooks_still_move_payments_forward(
    database_url: str, manifests: Path
) -> None:
    found = run_in_fresh_database(
        database_url, manifests, load(SCENARIOS / "ach-pull-out-of-order.yaml")
    )

    # Settled heard first: both steps booked by the settlement event, and the late
    # submission is stale.
    cleo = payment(found, "cleo-funding")
    assert states(found, "cleo-funding") == ["created", "submitted", "settled", "available"]
    assert outcomes(found, "cleo-funding") == ["applied", "stale"]
    settled_event = cleo["inbox_outcomes"][0]["event_id"]
    assert [step["actor"] for step in cleo["transitions"][1:3]] == [settled_event] * 2

    # Declined heard first: nothing was ever pending, so nothing is reversed. The late
    # submission reports a state the Payment never reached: an exception, no Ledger write.
    assert states(found, "dov-funding") == ["created", "declined"]
    assert outcomes(found, "dov-funding") == ["applied", "exception"]
    assert [step["transaction"] for step in payment(found, "dov-funding")["transitions"]] == [
        None,
        None,
    ]

    # Six hours late, the settlement still takes effect when it happened (Friday 08:30
    # New York), recorded when the Ledger heard of it.
    top_up = payment(found, "cleo-top-up")["transitions"]
    assert [step["state"] for step in top_up] == ["created", "submitted", "settled", "available"]
    assert top_up[2]["effective_at"] == "2026-09-04T12:30:00+00:00"
    assert top_up[2]["recorded_at"] == "2026-09-04T18:30:00+00:00"
    assert outcomes(found, "cleo-top-up") == ["applied", "applied"]
    assert {f.kind for f in found.manifest.faults} == {"out_of_order", "delay"}


def test_a_declined_or_canceled_pull_runs_end_to_end_through_real_webhooks(
    database_url: str, manifests: Path
) -> None:
    found = run_in_fresh_database(
        database_url,
        manifests,
        Scenario.model_validate(
            {
                "name": "declined-and-canceled",
                "start": THURSDAY_3PM_NEW_YORK,
                "customers": [{"id": "c-gale", "name": "Gale Petrov"}],
                "payments": [
                    {"id": "declined", "customer": "c-gale", "amount": 5_000, "outcome": "decline"},
                    {"id": "canceled", "customer": "c-gale", "amount": 700, "outcome": "cancel"},
                ],
            }
        ),
    )

    for payment_id, ending in (("declined", "declined"), ("canceled", "canceled")):
        steps = payment(found, payment_id)["transitions"]
        assert [s["state"] for s in steps] == ["created", "submitted", ending]
        pending, reversal = (found.summary["transactions"][s["transaction"]] for s in steps[1:])
        assert reversal["reverses"] == steps[1]["transaction"]
        assert pending["status"] == reversal["status"] == "pending"
        assert sum(e[2] for e in pending["entries"] + reversal["entries"]) == 0
    assert found.summary["holds"] == {}


def test_a_run_writes_its_fault_manifest(database_url: str, tmp_path: Path) -> None:
    with fresh_database(database_url) as url:
        found = run(
            load(SCENARIOS / "ach-pull-out-of-order.yaml"), url, manifest_path=tmp_path / "m.json"
        )

    assert found.manifest_path == tmp_path / "m.json"
    written = json.loads((tmp_path / "m.json").read_text())

    assert (written["scenario"], written["seed"]) == ("ach-pull-out-of-order", 23)
    late = next(f for f in written["faults"] if f["fault_id"] == "settlement-six-hours-late")
    assert late == {
        "fault_id": "settlement-six-hours-late",
        "kind": "delay",
        "payment": "cleo-top-up",
        "idempotency_key": "scenario:ach-pull-out-of-order:23:cleo-top-up",
        "payment_id": late["payment_id"],
        "event_id": late["event_id"],
        "event_type": "payment.settled",
        "params": {"by": "PT6H"},
        "injected_at": "2026-09-04T12:30:00Z",
        "cause_code": "delayed_delivery",
        "root_cause": late["root_cause"],
    }
    assert late["payment_id"].startswith("simpay_")
    assert late["event_id"] in late["root_cause"]
    assert "6:00:00" in late["root_cause"]


def test_the_same_scenario_and_seed_replay_exactly(database_url: str, manifests: Path) -> None:
    scenario = load(SCENARIOS / "ach-pull-random-faults.yaml")

    first = run_in_fresh_database(database_url, manifests, scenario)
    second = run_in_fresh_database(database_url, manifests, scenario)
    # A database that already holds another Scenario's run: different sequences, same names.
    with fresh_database(database_url) as url:
        run(load(SCENARIOS / "ach-pull-duplicates.yaml"), url, manifest_path=manifests / "d.json")
        crowded = run(scenario, url, manifest_path=manifests / "crowded.json")
    other_seed = run_in_fresh_database(database_url, manifests, scenario, seed=scenario.seed + 1)

    assert first.manifest.faults  # the seed drew some Faults
    assert first.manifest == second.manifest == crowded.manifest
    assert first.ticks == second.ticks == crowded.ticks
    assert first.summary == second.summary == only(scenario.name, crowded.summary)
    assert other_seed.manifest.faults != first.manifest.faults


def only(name: str, summary: dict[str, Any]) -> dict[str, Any]:
    """The part of a Ledger summary belonging to one Scenario's Payments."""
    prefix = f"scenario:{name}:"
    return {
        section: {key: value for key, value in rows.items() if key.startswith(prefix)}
        for section, rows in summary.items()
    }


ALL_FAULTS = Scenario.model_validate(
    {
        "name": "every-delivery-fault",
        "start": THURSDAY_3PM_NEW_YORK,
        "customers": [{"id": "c-hana", "name": "Hana Lindqvist"}],
        "payments": [
            {"id": "settles", "customer": "c-hana", "amount": 18_000},
            {"id": "declines", "customer": "c-hana", "amount": 900, "outcome": "decline"},
            {
                "id": "cancels",
                "customer": "c-hana",
                "amount": 450,
                "outcome": "cancel",
                "at": "PT1H",
            },
        ],
        "faults": [
            {
                "id": "swap",
                "kind": "out_of_order",
                "payment": "settles",
                "event": "submitted",
                "after": "settled",
            },
            {"id": "twice", "kind": "duplicate", "payment": "settles", "event": "settled"},
            {
                "id": "late",
                "kind": "delay",
                "payment": "declines",
                "event": "declined",
                "by": "PT2H",
            },
            {
                "id": "again",
                "kind": "duplicate",
                "payment": "cancels",
                "event": "submitted",
                "times": 3,
            },
        ],
    }
)


def test_replaying_the_inbox_from_scratch_reproduces_the_ledger_exactly(
    database_url: str, fresh: str, manifests: Path
) -> None:
    with fresh_database(database_url) as source:
        original = run(ALL_FAULTS, source, manifest_path=manifests / "all.json")
        assert {f.kind for f in original.manifest.faults} == {"duplicate", "delay", "out_of_order"}

        owner: Engine = create_engine(source)
        target = make_engine(fresh, role=LEDGER_APP_ROLE)
        try:
            with owner.connect() as connection:
                job_runs = availability_job_runs(connection)
                received = set(connection.scalars(select(inbox.c.received_at)))
            # The availability job ran at moments no Provider Event arrived, so a replay that
            # only applied the Inbox would miss them: they come from the job's own records.
            assert job_runs and not set(job_runs) <= received
            replay_inbox(owner, target)
        finally:
            owner.dispose()
            target.dispose()

    replayed = create_engine(fresh)
    try:
        assert ledger_summary(replayed) == original.summary
    finally:
        replayed.dispose()


def test_an_auction_close_scenario_pays_sellers_standard_and_instant(
    database_url: str, manifests: Path
) -> None:
    found = run_in_fresh_database(
        database_url, manifests, load(SCENARIOS / "auction-close-payouts.yaml")
    )

    [close] = found.summary["auction_closes"].values()
    payouts = {account.split(":")[1]: p for account, p in close["payouts"].items()}
    assert {name: [s["step"] for s in p["steps"]] for name, p in payouts.items()} == {
        "c-bram": ["reserved", "standard", "released"],
        "c-cleo": ["reserved", "instant"],
        "c-dara": ["reserved", "standard", "released"],  # over the limit
        "c-emil": ["reserved", "instant", "standard", "released"],  # declined over RTP
    }
    assert payouts["c-cleo"]["steps"][0]["effective_at"] == "2026-09-03T21:00:00+00:00"
    released = {payouts[n]["steps"][-1]["effective_at"] for n in ("c-bram", "c-dara", "c-emil")}
    assert released == {"2026-09-09T04:00:00+00:00"}  # midnight New York, Wednesday

    by_key = found.summary["payments"]
    key = "auction:auction-close-payouts:3:thursday-evening:payout:customer:{}:usd:{}"
    assert [s["state"] for s in by_key[key.format("c-cleo", "rtp")]["transitions"]] == [
        "created",
        "settled",
    ]
    assert [o["outcome"] for o in by_key[key.format("c-cleo", "rtp")]["inbox_outcomes"]] == [
        "applied"
    ]
    assert [s["state"] for s in by_key[key.format("c-emil", "rtp")]["transitions"]] == [
        "created",
        "declined",
    ]
    for name in ("c-bram", "c-dara", "c-emil"):
        steps = by_key[key.format(name, "ach")]["transitions"]
        assert [s["state"] for s in steps] == ["created", "submitted", "settled"]
        assert steps[-1]["effective_at"] == "2026-09-10T12:30:00+00:00"
    assert key.format("c-dara", "rtp") not in by_key  # never sent over RTP
    assert {f.fault_id for f in found.manifest.faults} == {
        "cleo-settled-twice",
        "bram-submitted-late",
    }
    assert no_exceptions(found)
    assert clearing_totals(found.summary) == {
        "clearing:simulator:ach:usd": 0,
        "clearing:simulator:rtp:usd": 0,
    }


def clearing_totals(summary: dict[str, Any]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for transaction in summary["transactions"].values():
        for account, _currency, amount, *_ in transaction["entries"]:
            if account.startswith("clearing:"):
                totals[account] = totals.get(account, 0) + amount
    return totals


def test_replaying_the_inbox_reproduces_a_ledger_with_payouts(
    database_url: str, fresh: str, manifests: Path
) -> None:
    with fresh_database(database_url) as source:
        original = run(
            load(SCENARIOS / "auction-close-payouts.yaml"),
            source,
            manifest_path=manifests / "payouts.json",
        )
        owner: Engine = create_engine(source)
        target = make_engine(fresh, role=LEDGER_APP_ROLE)
        try:
            replay_inbox(owner, target)
        finally:
            owner.dispose()
            target.dispose()

    assert original.summary["auction_closes"]
    replayed = create_engine(fresh)
    try:
        assert ledger_summary(replayed) == original.summary
    finally:
        replayed.dispose()
