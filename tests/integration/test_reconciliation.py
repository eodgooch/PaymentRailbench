"""Reconciliation end to end (SPEC §7.1, ADR 0015): every Fault from the Simulator ends as
the Break its kind calls for, or as none when the Ledger absorbs it; re-running a day finds
the same Breaks; a person resolves a Break once, with a correcting Transaction or a reason;
and the Exception Queue reads largest exposure first, then oldest.

Two Scenario runs serve the whole module, each in a database kept for it: the Settlement
Report Faults Scenario (and its late settlement webhook) and one with every delivery Fault
and a Return. The tests read the Fault Manifest to know what was injected; the Ledger never
does."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import psycopg.errors
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import DBAPIError

from railbench.common.clock import ManualClock
from railbench.common.db import LEDGER_APP_ROLE, make_engine
from railbench.ledger.app import create_app as create_ledger_app
from railbench.ledger.journal import Journal
from railbench.ledger.model import NewEntry, NewTransaction, Person
from railbench.payments.reports import SettlementReports
from railbench.reconciliation.matching import BreakType
from railbench.reconciliation.model import BreakStatus, NewResolution, Resolver
from railbench.reconciliation.service import (
    BreakNotFound,
    CorrectingTransactionNotFound,
    NotReady,
    Reconciliation,
    ResolutionConflict,
)
from railbench.reconciliation.tables import break_evidence, breaks, runs
from railbench.scenarios.model import Scenario, load
from railbench.scenarios.replay import RecordedProvider
from railbench.scenarios.runner import Run, fresh_database, run

pytestmark = pytest.mark.integration

SCENARIOS = Path(__file__).parents[2] / "scenarios"
FRIDAY, TUESDAY = date(2026, 9, 4), date(2026, 9, 8)

DELIVERY_FAULTS = Scenario.model_validate(
    {
        "name": "every-delivery-fault-reconciled",
        "start": "2026-09-03T15:00:00-04:00",
        "customers": [{"id": "c-iris", "name": "Iris Fenwick"}],
        "payments": [
            {"id": "twice", "customer": "c-iris", "amount": 1_000},
            {"id": "delayed", "customer": "c-iris", "amount": 2_000},
            {"id": "swapped", "customer": "c-iris", "amount": 3_000},
            {"id": "declined", "customer": "c-iris", "amount": 4_000, "outcome": "decline"},
            {"id": "returned", "customer": "c-iris", "amount": 5_000},
        ],
        "faults": [
            {"id": "settled-twice", "kind": "duplicate", "payment": "twice", "event": "settled"},
            {
                "id": "settled-late",
                "kind": "delay",
                "payment": "delayed",
                "event": "settled",
                "by": "PT6H",
            },
            {
                "id": "submitted-after-settled",
                "kind": "out_of_order",
                "payment": "swapped",
                "event": "submitted",
                "after": "settled",
            },
            {
                "id": "submitted-after-declined",
                "kind": "out_of_order",
                "payment": "declined",
                "event": "submitted",
                "after": "declined",
            },
            {"id": "r01", "kind": "return", "payment": "returned", "code": "R01", "at": "PT3H"},
        ],
    }
)


@dataclass(frozen=True)
class Kept:
    """A Scenario run and the database it ran in, kept for the module."""

    url: str
    found: Run


def _kept(database_url: str, tmp: Path, scenario: Scenario) -> Iterator[Kept]:
    with fresh_database(database_url, prefix="reconcile") as url:
        yield Kept(url, run(scenario, url, manifest_path=tmp / f"{scenario.name}.json"))


@pytest.fixture(scope="module")
def report_faults(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Kept]:
    yield from _kept(
        database_url,
        tmp_path_factory.mktemp("manifests"),
        load(SCENARIOS / "settlement-report-faults.yaml"),
    )


@pytest.fixture(scope="module")
def delivery_faults(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Kept]:
    yield from _kept(database_url, tmp_path_factory.mktemp("manifests"), DELIVERY_FAULTS)


def found_breaks(kept: Kept) -> dict[str, Any]:
    found: dict[str, Any] = kept.found.summary["reconciliation"]["breaks"]
    return found


def about(kept: Kept, payment: str) -> set[tuple[str, str, int]]:
    """The Breaks about one of the Scenario's Payments: (day, Break Type, exposure)."""
    key = kept.found.scenario.plans(kept.found.seed)[payment].idempotency_key
    return {
        (b["run"].split(":")[1], b["break_type"], b["exposure"])
        for b in found_breaks(kept).values()
        if key in b["evidence"].get("payments", [])
    }


# What each Fault becomes (ADR 0015's table): a Break Type, or None when the Ledger agrees.
REPORT_FAULTS: dict[str, str | None] = {
    "wren-reported-short": "amount_mismatch",
    "pax-line-missing": "missing_at_provider",
    "jules-after-cutoff": "timing_mismatch",
    # Booked against its Payment: the Ledger agrees with the report.
    "ottoline-payout-recalled": None,
    "wren-fee-corrected": None,
    # Booked once the late webhook landed, before Tuesday's report was reconciled.
    "rosa-fee-corrected": None,
    # Five days late, past Friday's Reconciliation (on Tuesday's report, 20:00 New York).
    "rosa-settled-late": "status_mismatch",
}


@pytest.mark.parametrize("fault_id", sorted(REPORT_FAULTS))
def test_each_report_fault_ends_as_its_break(report_faults: Kept, fault_id: str) -> None:
    faults = report_faults.found.manifest.faults
    [fault] = [f for f in faults if f.fault_id == fault_id]
    expected = REPORT_FAULTS[fault_id]

    types = {kind for _, kind, _ in about(report_faults, fault.payment)}

    # The Payment's Breaks are exactly those its Faults call for, this one's among them.
    # (The orphan adjustment only borrows a Payment's name; it has a test of its own.)
    assert types == {
        REPORT_FAULTS.get(f.fault_id) for f in faults if f.payment == fault.payment
    } - {None}
    assert expected is None or expected in types


def test_an_orphan_adjustment_is_missing_in_ledger(report_faults: Kept) -> None:
    [fault] = [f for f in report_faults.found.manifest.faults if f.kind == "orphan_adjustment"]
    [orphan] = [b for b in found_breaks(report_faults).values() if b["match_key"] == fault.event_id]

    assert (orphan["run"], orphan["break_type"], orphan["exposure"]) == (
        "simulator:2026-09-08",
        "missing_in_ledger",
        450,
    )
    assert "payments" not in orphan["evidence"]  # it names no Payment the Ledger has
    [decision] = orphan["evidence"]["provider_adjustments"]
    assert decision.endswith("/unmatched@2026-09-09T00:00:00+00:00")


def test_the_report_faults_scenario_has_exactly_these_breaks(report_faults: Kept) -> None:
    found = sorted(
        (b["run"], b["break_type"], b["exposure"]) for b in found_breaks(report_faults).values()
    )

    assert found == [
        ("simulator:2026-09-04", "amount_mismatch", 500),
        ("simulator:2026-09-04", "missing_at_provider", 12_000),
        ("simulator:2026-09-04", "status_mismatch", 5_000),
        ("simulator:2026-09-04", "timing_mismatch", 8_000),
        ("simulator:2026-09-08", "missing_in_ledger", 450),
    ]
    # Every Banking Day with anything on it is reconciled, each once its next is in.
    reconciled = report_faults.found.summary["reconciliation"]["runs"]
    assert list(reconciled) == [
        "simulator:2026-09-03",
        "simulator:2026-09-04",
        "simulator:2026-09-08",
        "simulator:2026-09-09",
    ]
    assert reconciled["simulator:2026-09-04"]["known_at"] == "2026-09-09T00:00:00+00:00"


@pytest.mark.parametrize(
    "fault_id",
    ["settled-twice", "settled-late", "submitted-after-settled", "submitted-after-declined", "r01"],
)
def test_delivery_faults_and_returns_are_absorbed_by_the_ledger_and_raise_no_break(
    delivery_faults: Kept, fault_id: str
) -> None:
    [fault] = [f for f in delivery_faults.found.manifest.faults if f.fault_id == fault_id]

    assert about(delivery_faults, fault.payment) == set()


def test_a_run_with_only_delivery_faults_has_no_breaks(delivery_faults: Kept) -> None:
    assert {f.kind for f in delivery_faults.found.manifest.faults} == {
        "duplicate",
        "delay",
        "out_of_order",
        "return",
    }
    assert found_breaks(delivery_faults) == {}
    assert delivery_faults.found.summary["reconciliation"]["runs"]


def test_a_breaks_evidence_names_its_payment_inbox_events_transactions_and_lines(
    report_faults: Kept,
) -> None:
    [timing] = [
        b for b in found_breaks(report_faults).values() if b["break_type"] == "timing_mismatch"
    ]
    jules = "scenario:settlement-report-faults:8:jules-funding"

    assert timing["evidence"]["payments"] == [jules]
    assert f"{jules}/settled" in timing["evidence"]["transitions"]
    assert len(timing["evidence"]["inbox"]) == 2  # submitted and settled
    assert f"{jules}/pending" in timing["evidence"]["transactions"]
    [line] = timing["evidence"]["report_lines"]
    assert line.startswith("simulator:2026-09-08:")
    assert timing["facts"]["expected_report_day"] == "2026-09-04"
    assert timing["facts"]["reported_on"] == "2026-09-08"


@pytest.fixture
def stack(report_faults: Kept) -> Iterator[tuple[Reconciliation, ManualClock, Engine]]:
    """Reconciliation over the report Faults run's database, as the app role, on a Clock
    after the run."""
    engine = make_engine(report_faults.url, role=LEDGER_APP_ROLE)
    clock = ManualClock(report_faults.found.ticks[-1] + timedelta(days=1))
    try:
        yield Reconciliation(engine, clock, RecordedProvider("simulator", {})), clock, engine
    finally:
        engine.dispose()


def count(engine: Engine, table: Any) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def test_re_running_a_day_finds_the_same_breaks_and_adds_nothing(
    stack: tuple[Reconciliation, ManualClock, Engine],
) -> None:
    reconciliation, _, engine = stack
    before = {table.name: count(engine, table) for table in (runs, breaks, break_evidence)}
    first = reconciliation.break_(reconciliation.breaks(banking_day=FRIDAY)[0].id)

    again = reconciliation.reconcile(FRIDAY)

    assert len(again.breaks) == 4
    assert {b.id for b in reconciliation.breaks(status=None, banking_day=FRIDAY)} == set(
        again.breaks
    )
    assert {table.name: count(engine, table) for table in (runs, breaks, break_evidence)} == before
    assert reconciliation.break_(first.id).detected_at == first.detected_at
    assert reconciliation.reconcile_due() == []  # nothing left to do


def test_two_runs_of_one_day_at_once_record_it_once(
    stack: tuple[Reconciliation, ManualClock, Engine],
) -> None:
    reconciliation, _, engine = stack
    barrier = Barrier(2)

    def go() -> list[str]:
        barrier.wait()
        return reconciliation.reconcile(TUESDAY).breaks

    with ThreadPoolExecutor(2) as pool:
        results = [f.result() for f in [pool.submit(go), pool.submit(go)]]

    assert results[0] == results[1] and len(results[0]) == 1
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count()).select_from(runs).where(runs.c.banking_day == TUESDAY)
            )
            == 1
        )


def test_a_day_is_not_reconciled_before_its_next_banking_days_report_is_in(
    stack: tuple[Reconciliation, ManualClock, Engine],
) -> None:
    reconciliation, _, _ = stack

    with pytest.raises(NotReady, match="2026-09-11"):
        reconciliation.reconcile(date(2026, 9, 10))  # Friday 11's report never came
    status = reconciliation.day_status(date(2026, 9, 10))
    assert (status.reconciled, status.breaks) == (False, 0)


def test_the_exception_queue_reads_largest_exposure_first_then_oldest(
    stack: tuple[Reconciliation, ManualClock, Engine],
) -> None:
    reconciliation, _, _ = stack

    queue = reconciliation.breaks()
    open_ones = [b for b in queue if b.status is BreakStatus.OPEN]

    assert open_ones == queue
    exposures = [(b.exposure, b.detected_at) for b in queue]
    assert exposures == sorted(exposures, key=lambda e: (-e[0], e[1]))
    assert [b.break_type for b in reconciliation.breaks(break_type=BreakType.TIMING_MISMATCH)] == [
        BreakType.TIMING_MISMATCH
    ]
    assert reconciliation.breaks(provider="elsewhere") == []


def test_a_breaks_age_runs_from_detection_on_the_ledgers_clock(
    stack: tuple[Reconciliation, ManualClock, Engine],
) -> None:
    reconciliation, clock, _ = stack
    [orphan] = reconciliation.breaks(break_type=BreakType.MISSING_IN_LEDGER)
    detected = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)  # Wednesday's report, 20:00 New York

    assert orphan.detected_at == detected
    clock.set(detected + timedelta(hours=30))
    assert reconciliation.break_(orphan.id).age_seconds == 30 * 3600


def resolver(proposal: str | None = None) -> Resolver:
    return Resolver(operator_id="op-maren", proposal_id=proposal)


def test_resolving_with_a_reason_takes_a_break_off_the_queue_once(
    stack: tuple[Reconciliation, ManualClock, Engine],
) -> None:
    reconciliation, clock, _ = stack
    [status] = reconciliation.breaks(break_type=BreakType.STATUS_MISMATCH)
    before = reconciliation.day_status(FRIDAY)
    new = NewResolution(actor=resolver(), reason="the settlement webhook arrived on 9 September")

    resolved, replayed = reconciliation.resolve(status.id, new)

    assert (resolved.status, replayed) == (BreakStatus.RESOLVED, False)
    assert resolved.resolution is not None and resolved.resolution.recorded_at == clock.now()
    assert status.id not in {b.id for b in reconciliation.breaks()}
    assert status.id in {b.id for b in reconciliation.breaks(status=BreakStatus.RESOLVED)}
    after = reconciliation.day_status(FRIDAY)
    assert after.open_breaks == before.open_breaks - 1
    assert after.open_exposure["USD"] == before.open_exposure["USD"] - 5_000
    # The same resolution again is a retry; another one is a conflict.
    assert reconciliation.resolve(status.id, new)[1] is True
    with pytest.raises(ResolutionConflict):
        reconciliation.resolve(status.id, NewResolution(actor=resolver(), reason="other"))
    # Its age stopped when it was resolved.
    clock.advance(timedelta(days=3))
    assert reconciliation.break_(status.id).age_seconds == resolved.age_seconds


def test_resolving_with_a_correcting_transaction_links_one_that_is_in_the_ledger(
    stack: tuple[Reconciliation, ManualClock, Engine],
) -> None:
    reconciliation, clock, engine = stack
    [amount] = reconciliation.breaks(break_type=BreakType.AMOUNT_MISMATCH)
    with pytest.raises(CorrectingTransactionNotFound):
        reconciliation.resolve(
            amount.id, NewResolution(actor=resolver(), correcting_transaction_id=10**12)
        )
    correction = (
        Journal(engine, clock)
        .post(
            NewTransaction(
                idempotency_key="correction:wren-short",
                actor=Person(operator_id="op-maren", proposal_id="prop-1"),
                effective_at=clock.now(),
                description="The Provider's 500 short on Wren's line, borne as Provider Fees",
                entries=[
                    NewEntry(account="provider-fees:usd", currency="USD", amount=500),
                    NewEntry(account="bank-cash:usd", currency="USD", amount=-500),
                ],
            )
        )
        .transaction
    )

    resolved, _ = reconciliation.resolve(
        amount.id,
        NewResolution(actor=resolver("prop-1"), correcting_transaction_id=correction.id),
    )

    assert resolved.resolution is not None
    assert resolved.resolution.correcting_transaction_id == correction.id
    assert resolved.resolution.actor.proposal_id == "prop-1"


@pytest.mark.parametrize(
    "body",
    [
        {"actor": {"operator_id": "op-maren"}},  # neither
        {"actor": {"operator_id": "op-maren"}, "reason": "x", "correcting_transaction_id": 1},
        {"actor": {"operator_id": "op-maren"}, "reason": "   "},
        {"actor": {"operator_id": ""}, "reason": "x"},
        {"reason": "no actor"},
        {"actor": {"kind": "system_job", "operator_id": "job"}, "reason": "x"},
    ],
)
def test_a_resolution_needs_a_person_and_exactly_one_of_a_transaction_or_a_reason(
    body: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        NewResolution.model_validate(body)


def test_resolving_an_unknown_break_is_refused(
    stack: tuple[Reconciliation, ManualClock, Engine],
) -> None:
    reconciliation, _, _ = stack

    with pytest.raises(BreakNotFound):
        reconciliation.resolve("brk_" + "0" * 24, NewResolution(actor=resolver(), reason="x"))


def test_the_api_serves_the_queue_a_break_and_a_days_status(report_faults: Kept) -> None:
    engine = make_engine(report_faults.url, role=LEDGER_APP_ROLE)
    clock = ManualClock(report_faults.found.ticks[-1])
    try:
        client = TestClient(create_ledger_app(engine, clock))
        queue = client.get("/breaks", params={"banking_day": "2026-09-08"})
        assert queue.status_code == 200
        [orphan] = queue.json()
        assert orphan["break_type"] == "missing_in_ledger"
        one = client.get(f"/breaks/{orphan['id']}").json()
        assert one["evidence"]["report_lines"] and one["evidence"]["provider_adjustments"]
        assert client.get("/breaks/brk_unknown").status_code == 404
        assert client.get("/breaks", params={"status": "all"}).status_code == 200
        status = client.get("/reconciliations/simulator/2026-09-08").json()
        assert (status["reconciled"], status["breaks"]) == (True, 1)
        assert client.post("/reconciliations/simulator/2026-09-08").json()["breaks"] == [
            orphan["id"]
        ]
        assert client.post("/reconciliations/simulator/2026-09-11").status_code == 409

        url = f"/breaks/{orphan['id']}/resolution"
        body = {"actor": {"operator_id": "op-maren"}, "reason": "a stranger's fee: disputed"}
        assert client.post(url, json={"actor": {"operator_id": "op-maren"}}).status_code == 422
        assert client.post(url, json=body).status_code == 201
        again = client.post(url, json=body)
        assert (again.status_code, again.headers["Idempotent-Replayed"]) == (200, "true")
        assert client.post(url, json=body | {"reason": "other"}).status_code == 409
        wrong = body | {"reason": None, "correcting_transaction_id": 10**12}
        other = client.get("/breaks", params={"banking_day": "2026-09-04"}).json()[0]
        assert client.post(f"/breaks/{other['id']}/resolution", json=wrong).status_code == 422
    finally:
        engine.dispose()


def test_a_reissued_report_is_a_duplicate_break_and_a_line_for_no_payment_missing_in_ledger(
    database_url: str,
) -> None:
    """A Provider re-sending a day's report with other lines (kept as a conflict, ADR 0014)
    is a duplicate; a malformed one holds Reconciliation up until a readable one arrives."""
    header = (
        "report_date,provider,line_id,type,provider_payment_id,idempotency_key,rail,direction,"
        "currency,amount,settled_at,original_line_id,reason\n"
    )

    def body(day: date, *amounts: int) -> bytes:
        rows = "".join(
            f"{day},simulator,simline_{n},settlement,simpay_{n},key-{n},ach,pull,USD,{amount},"
            f"{day}T12:30:00Z,,\n"
            for n, amount in enumerate(amounts)
        )
        return (header + rows).encode()

    with fresh_database(database_url, prefix="reissue") as url:
        engine = make_engine(url, role=LEDGER_APP_ROLE)
        try:
            clock = ManualClock(datetime(2026, 9, 5, 0, 0, tzinfo=UTC))
            provider = RecordedProvider("simulator", {})
            reports = SettlementReports(engine, clock, Journal(engine, clock), provider)
            reconciliation = Reconciliation(engine, clock, provider)
            reports.replay("simulator", FRIDAY, body(FRIDAY, 700), {})
            reports.replay("simulator", FRIDAY, body(FRIDAY, 900, 300), {})  # a re-issue
            clock.set(datetime(2026, 9, 9, 0, 0, tzinfo=UTC))
            reports.replay("simulator", TUESDAY, b"not,a,report\n", {})
            assert reconciliation.reconcile_due() == []
            with pytest.raises(NotReady):
                reconciliation.reconcile(FRIDAY)
            reports.replay("simulator", TUESDAY, body(TUESDAY), {})

            [ran] = reconciliation.reconcile_due()

            found = {(b.break_type, b.exposure) for b in reconciliation.breaks()}
            assert found == {
                (BreakType.MISSING_IN_LEDGER, 700),  # simpay_0: no such Payment
                (BreakType.DUPLICATE, 200 + 300),  # 700 restated as 900, and a new 300
            }
            assert ran.known_at == clock.now()
        finally:
            engine.dispose()


# The schema's defences (migration 0010).

COLUMNS = {
    "runs": "job_name",
    "breaks": "match_key",
    "break_evidence": "break_id",
    "break_resolutions": "reason",
}
EDITS = {
    "update": "UPDATE reconciliation.{table} SET {column} = {column}",
    "delete": "DELETE FROM reconciliation.{table}",
    "truncate": "TRUNCATE reconciliation.{table} CASCADE",
}


@pytest.mark.parametrize("table", sorted(COLUMNS))
@pytest.mark.parametrize("edit", sorted(EDITS))
def test_the_app_role_may_not_edit_reconciliation_history(
    app_engine: Engine, table: str, edit: str
) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        connection.execute(text(EDITS[edit].format(table=table, column=COLUMNS[table])))

    assert type(raised.value.orig) is psycopg.errors.InsufficientPrivilege


@pytest.mark.parametrize("table", sorted(COLUMNS))
@pytest.mark.parametrize("edit", sorted(EDITS))
def test_the_trigger_stops_even_the_owner_editing_reconciliation_history(
    engine: Engine, table: str, edit: str
) -> None:
    with pytest.raises(DBAPIError) as raised, engine.begin() as connection:
        connection.execute(text(EDITS[edit].format(table=table, column=COLUMNS[table])))

    assert type(raised.value.orig) is psycopg.errors.RestrictViolation


def test_the_database_holds_a_resolution_to_exactly_one_of_a_transaction_or_a_reason(
    report_faults: Kept,
) -> None:
    owner = create_engine(report_faults.url)
    try:
        some_break = found_breaks(report_faults)
        identity = next(iter(some_break))
        for values in ("NULL, NULL", "1, 'both'"):
            with pytest.raises(DBAPIError) as raised, owner.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO reconciliation.break_resolutions (break_id, "
                        "actor_operator_id, correcting_transaction_id, reason, recorded_at) "
                        f"VALUES (:id, 'op', {values}, now())"
                    ),
                    {"id": identity},
                )
            assert type(raised.value.orig) is psycopg.errors.CheckViolation
    finally:
        owner.dispose()
