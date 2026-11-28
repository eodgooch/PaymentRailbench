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
from railbench.scenarios.runner import DatabaseNotFresh, Run, fresh_database, run
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


def no_breaks(found: Run) -> bool:
    """Reconciliation found nothing: delivery Faults and Returns are the Ledger's to absorb,
    never Breaks (ADR 0015)."""
    reconciled = found.summary["reconciliation"]
    return bool(reconciled["runs"]) and reconciled["breaks"] == {}


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
    assert no_breaks(found)


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
    assert no_breaks(found)


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
    other_seed = run_in_fresh_database(database_url, manifests, scenario, seed=scenario.seed + 1)

    assert first.manifest.faults  # the seed drew some Faults
    assert first.manifest == second.manifest
    assert first.ticks == second.ticks
    assert first.settlement_reports and first.settlement_reports == second.settlement_reports
    assert first.summary == second.summary
    assert other_seed.manifest.faults != first.manifest.faults
    assert no_breaks(first) and no_breaks(other_seed)


def test_a_scenario_refuses_a_database_that_is_not_fresh(
    database_url: str, manifests: Path
) -> None:
    """A Settlement Report covers every Payment its Provider settled that day and is frozen
    once published, so a second run in one database would find its days' reports published
    without its settlements. Determinism holds per fresh database (ADR 0011, ADR 0014)."""
    with fresh_database(database_url) as url:
        run(load(SCENARIOS / "ach-pull-duplicates.yaml"), url, manifest_path=manifests / "d.json")
        with pytest.raises(DatabaseNotFresh, match=r"simulator\.settlement_reports"):
            run(load(SCENARIOS / "ach-pull-random-faults.yaml"), url, manifest_path=manifests / "x")


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
    assert no_breaks(found)
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


def account_totals(summary: dict[str, Any]) -> dict[str, int]:
    """Every Account's Entries summed, signed debit-positive (a liability in credit reads
    negative)."""
    totals: dict[str, int] = {}
    for transaction in summary["transactions"].values():
        for account, _currency, amount, *_ in transaction["entries"]:
            totals[account] = totals.get(account, 0) + amount
    return totals


def test_the_headline_late_r10_after_an_instant_payout_ends_in_recovery_receivable(
    database_url: str, manifests: Path
) -> None:
    found = run_in_fresh_database(
        database_url, manifests, load(SCENARIOS / "late-r10-after-instant-payout.yaml")
    )

    imogen, tobias = "customer:c-imogen:usd", "customer:c-tobias:usd"
    totals = account_totals(found.summary)
    assert totals[imogen] == 0
    assert totals["recovery-receivable:customer:c-imogen:usd"] == 30_000
    assert totals["bank-cash:usd"] == -27_000  # in 300.00, back out 300.00, paid out 270.00
    assert totals["fee-revenue:usd"] == -3_000
    assert totals[tobias] == 0  # paid in full, untouched by the Return
    assert totals["clearing:simulator:ach:usd"] == totals["clearing:simulator:rtp:usd"] == 0

    funding = payment(found, "imogen-funding")
    assert states(found, "imogen-funding") == [
        "created",
        "submitted",
        "settled",
        "available",
        "returned",
    ]
    assert outcomes(found, "imogen-funding") == ["applied", "applied", "applied"]
    returned = funding["transitions"][-1]
    assert returned["effective_at"] == "2026-10-06T12:30:00+00:00"
    reversal = found.summary["transactions"][returned["transaction"]]
    assert reversal["reverses"] == funding["transitions"][1]["transaction"]
    [exception] = funding["return_exceptions"]
    assert (exception["return_code"], exception["late"], exception["exposure"]) == (
        "R10",
        True,
        30_000,
    )
    assert exception["recovery_account"] == "recovery-receivable:customer:c-imogen:usd"

    [close] = found.summary["auction_closes"].values()
    assert [s["step"] for s in close["payouts"][tobias]["steps"]] == ["reserved", "instant"]
    assert [(f.fault_id, f.kind, f.cause_code) for f in found.manifest.faults] == [
        ("imogen-r10", "return", "late_return"),
        ("return-delivered-twice", "duplicate", "duplicate_delivery"),
    ]
    assert "R10" in found.manifest.faults[0].root_cause
    assert no_breaks(found)  # a Return isn't on a report, and its line stays on its day


def test_a_settlement_redelivered_after_a_return_changes_nothing(
    database_url: str, manifests: Path
) -> None:
    found = run_in_fresh_database(
        database_url, manifests, load(SCENARIOS / "duplicate-settlement-after-return.yaml")
    )

    assert states(found, "kit-funding") == ["created", "submitted", "settled", "returned"]
    # Three events, each applied once; the settlement's redelivery found its Inbox row.
    assert outcomes(found, "kit-funding") == ["applied", "applied", "applied"]
    assert {f.fault_id for f in found.manifest.faults} == {"kit-r01", "settlement-redelivered"}
    totals = account_totals(found.summary)
    assert totals["customer:c-kit:usd"] == totals["bank-cash:usd"] == 0
    assert totals["clearing:simulator:ach:usd"] == 0
    assert "recovery-receivable:customer:c-kit:usd" not in totals
    [hold] = found.summary["holds"].values()
    assert hold["end"]["effective_at"] == "2026-09-07T16:00:00+00:00"  # ended by the Return
    assert payment(found, "kit-funding")["return_exceptions"] == []
    assert no_breaks(found)


@pytest.mark.parametrize(
    "name", ["late-r10-after-instant-payout", "duplicate-settlement-after-return"]
)
def test_replaying_the_inbox_reproduces_a_ledger_with_returns(
    database_url: str, fresh: str, manifests: Path, name: str
) -> None:
    with fresh_database(database_url) as source:
        original = run(load(SCENARIOS / f"{name}.yaml"), source, manifest_path=manifests / "r.json")
        owner: Engine = create_engine(source)
        target = make_engine(fresh, role=LEDGER_APP_ROLE)
        try:
            replay_inbox(owner, target)
        finally:
            owner.dispose()
            target.dispose()

    replayed = create_engine(fresh)
    try:
        assert ledger_summary(replayed) == original.summary
    finally:
        replayed.dispose()


# Settlement Reports (ADR 0014)


def report_lines(found: Run, day: str) -> list[tuple[str, str, int, str]]:
    [report] = found.summary["settlement_reports"][f"simulator:{day}"]
    assert report["outcome"] == "ingested"
    return [
        (line["type"], line["idempotency_key"].split(":")[-1], line["amount"], line["settled_at"])
        for line in report["lines"]
    ]


def adjustment_decisions(found: Run, day: str) -> dict[str, list[tuple[str, str | None]]]:
    """Each adjustment line's decisions on that day's report, by the Scenario's name for
    its Payment ("" for one the Ledger never made): (outcome, the Payment it names)."""
    [report] = found.summary["settlement_reports"][f"simulator:{day}"]
    return {
        line["idempotency_key"].split(":")[-1]: [
            (decision["outcome"], decision["payment"]) for decision in line["adjustments"]
        ]
        for line in report["lines"]
        if line["type"] == "adjustment"
    }


def test_every_settlement_report_fault_shows_on_its_report_and_in_the_manifest(
    database_url: str, manifests: Path
) -> None:
    found = run_in_fresh_database(
        database_url, manifests, load(SCENARIOS / "settlement-report-faults.yaml")
    )
    key = "scenario:settlement-report-faults:8:{}".format

    reports = found.summary["settlement_reports"]
    assert list(reports) == [
        "simulator:2026-09-03",
        "simulator:2026-09-04",
        "simulator:2026-09-08",
        "simulator:2026-09-09",
        # Empty, fetched so Wednesday 9 can be reconciled (ADR 0015).
        "simulator:2026-09-10",
    ]
    assert report_lines(found, "2026-09-10") == []
    assert report_lines(found, "2026-09-03") == [
        ("settlement", "rtp", -27_000, "2026-09-03T21:00:00+00:00"),
        ("settlement", "rtp", -27_000, "2026-09-03T21:00:00+00:00"),  # listed twice
    ]
    assert sorted(report_lines(found, "2026-09-04")) == [
        ("adjustment", "rtp", 2_000, "2026-09-04T16:00:00+00:00"),
        ("settlement", "rosa-funding", 5_000, "2026-09-04T12:30:00+00:00"),
        ("settlement", "wren-funding", 59_500, "2026-09-04T12:30:00+00:00"),
    ]
    assert sorted(report_lines(found, "2026-09-08")) == [
        ("adjustment", "", -450, "2026-09-08T16:00:00+00:00"),
        ("adjustment", "rosa-funding", -100, "2026-09-08T16:00:00+00:00"),
        ("settlement", "jules-funding", 8_000, "2026-09-04T20:00:00+00:00"),
    ]
    assert report_lines(found, "2026-09-09") == [
        ("adjustment", "wren-funding", -150, "2026-09-09T16:00:00+00:00")
    ]
    assert {(f.fault_id, f.payment, f.cause_code) for f in found.manifest.faults} == {
        ("wren-reported-short", "wren-funding", "wrong_report_amount"),
        ("pax-line-missing", "pax-funding", "missing_report_line"),
        ("jules-after-cutoff", "jules-funding", "settlement_after_cutoff"),
        ("ottoline-listed-twice", "ottoline-payout:rtp", "duplicate_report_line"),
        ("ottoline-payout-recalled", "ottoline-payout:rtp", "provider_adjustment"),
        ("wren-fee-corrected", "wren-funding", "provider_adjustment"),
        ("rosa-settled-late", "rosa-funding", "delayed_delivery"),
        ("rosa-fee-corrected", "rosa-funding", "provider_adjustment"),
        ("stranger-fee-corrected", "pax-funding", "orphan_adjustment"),
    }

    # Rosa's fee correction waited for her late settlement, then was booked, once; the
    # orphan was never booked.
    assert adjustment_decisions(found, "2026-09-08") == {
        "rosa-funding": [("pending", key("rosa-funding")), ("booked", key("rosa-funding"))],
        "": [("unmatched", None)],
    }
    [report] = reports["simulator:2026-09-08"]
    rosa = next(line for line in report["lines"] if line["idempotency_key"] == key("rosa-funding"))
    assert [d["recorded_at"] for d in rosa["adjustments"]] == [
        "2026-09-09T00:00:00+00:00",  # the report, Tuesday 20:00 New York
        "2026-09-09T12:30:00+00:00",  # the late webhook, Wednesday 08:30 New York
    ]
    assert adjustment_decisions(found, "2026-09-09") == {
        "wren-funding": [("booked", key("wren-funding"))]
    }
    payout = (
        "auction:settlement-report-faults:8:thursday-evening:payout:customer:c-ottoline:usd:rtp"
    )
    assert adjustment_decisions(found, "2026-09-04") == {"rtp": [("booked", payout)]}

    totals = account_totals(found.summary)
    assert totals["bank-cash:usd"] == 59_750
    assert totals["provider-fees:usd"] == 250
    assert totals["customer:c-ottoline:usd"] == -2_000  # owed again: a liability in credit
    assert totals["customer:c-pax:usd"] == -12_000  # funded in full, line or no line
    assert totals["customer:c-jules:usd"] == -8_000
    assert totals["customer:c-rosa:usd"] == -5_000  # the fee is Mintwharf's, not hers
    assert totals["clearing:simulator:ach:usd"] == totals["clearing:simulator:rtp:usd"] == 0
    assert no_exceptions(found)


def test_the_same_scenario_and_seed_publish_byte_identical_reports(
    database_url: str, manifests: Path
) -> None:
    scenario = load(SCENARIOS / "settlement-report-faults.yaml")

    first = run_in_fresh_database(database_url, manifests, scenario)
    second = run_in_fresh_database(database_url, manifests, scenario)

    assert first.settlement_reports
    assert first.settlement_reports == second.settlement_reports
    assert first.manifest == second.manifest
    assert first.summary == second.summary


def test_replaying_reproduces_a_ledger_with_settlement_reports_and_adjustments(
    database_url: str, fresh: str, manifests: Path
) -> None:
    with fresh_database(database_url) as source:
        original = run(
            load(SCENARIOS / "settlement-report-faults.yaml"),
            source,
            manifest_path=manifests / "reports.json",
        )
        owner: Engine = create_engine(source)
        target = make_engine(fresh, role=LEDGER_APP_ROLE)
        try:
            replay_inbox(owner, target)
        finally:
            owner.dispose()
            target.dispose()

    assert original.summary["settlement_reports"]
    assert original.summary["reconciliation"]["breaks"]  # replay reproduces them too
    replayed = create_engine(fresh)
    try:
        assert ledger_summary(replayed) == original.summary
    finally:
        replayed.dispose()
