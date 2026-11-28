"""The Ledger ingests each Settlement Report through the Provider interface (SPEC §4.5, §6,
ADR 0014): fetched once it's due, verified, stored raw and as lines, once per report;
a different report for the same day is kept as a conflict. Only a Provider Adjustment is
booked, against its original Payment; a missing, wrong or late line changes no balance.

Both services run in-process on one ManualClock over real HTTP, each test in its own
database (reports cover everything the Simulator ever settled)."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine

from railbench.common.clock import ManualClock
from railbench.common.db import LEDGER_APP_ROLE, make_engine
from railbench.common.settings import Settings
from railbench.ledger.app import create_app as create_ledger_app
from railbench.ledger.journal import Journal
from railbench.ledger.model import AccountType, NewAccount
from railbench.payments.model import ReportOutcome
from railbench.payments.reports import SettlementReports
from railbench.payments.service import Payments
from railbench.scenarios.runner import fresh_database
from railbench.scenarios.servers import free_port, serving
from railbench.simulator.app import create_app as create_simulator_app
from railbench.simulator.model import PaymentPlan
from railbench.simulator.provider import Simulator
from tests.integration.helpers import END_OF_TIME

pytestmark = pytest.mark.integration

SECRET = "ingest-secret"
THURSDAY_3PM = datetime(2026, 9, 3, 19, 0, tzinfo=UTC)
FRIDAY = date(2026, 9, 4)
FRIDAY_REPORT = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)  # 20:00 New York
TUESDAY_REPORT = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)  # after Labor Day
WEDNESDAY_REPORT = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
CUSTOMER = "customer:c-quinn:usd"
CLEARING, BANK_CASH, FEES = "clearing:simulator:ach:usd", "bank-cash:usd", "provider-fees:usd"


@dataclass
class Stack:
    clock: ManualClock
    ledger: httpx.Client
    simulator: Simulator
    payments: Payments
    reports: SettlementReports
    journal: Journal
    owner: Engine

    def at(self, moment: datetime) -> None:
        """Move the Clock and do what both running services do on their timers."""
        self.clock.set(moment)
        self.simulator.tick()
        self.simulator.publish_reports()
        self.payments.make_available()
        self.reports.ingest_due()

    def pull(self, key: str, amount: int, *faults: dict[str, Any]) -> int:
        self.simulator.plan(PaymentPlan.model_validate({"idempotency_key": key, "faults": faults}))
        response = self.ledger.post(
            "/payments",
            json={
                "idempotency_key": key,
                "rail": "ach",
                "direction": "pull",
                "customer_account": CUSTOMER,
                "amount": amount,
                "currency": "USD",
            },
        )
        assert response.status_code == 201, response.text
        payment_id: int = response.json()["id"]
        return payment_id

    def balance(self, code: str) -> int:
        return self.journal.balances(code, END_OF_TIME).posted

    def reports_for(self, day: str) -> list[dict[str, Any]]:
        response = self.ledger.get(f"/settlement-reports/simulator/{day}")
        assert response.status_code == 200, response.text
        found: list[dict[str, Any]] = response.json()
        return found


@pytest.fixture
def stack(database_url: str) -> Iterator[Stack]:
    with fresh_database(database_url, prefix="ingest") as url:
        owner = create_engine(url)
        app_engine = make_engine(url, role=LEDGER_APP_ROLE)
        clock = ManualClock(THURSDAY_3PM)
        ledger_port, simulator_port = free_port(), free_port()
        ledger_url = f"http://127.0.0.1:{ledger_port}"
        settings = Settings(
            simulator_url=f"http://127.0.0.1:{simulator_port}",
            ledger_webhook_url=f"{ledger_url}/providers/simulator/events",
            webhook_signing_secret=SECRET,
        )
        ledger_app = create_ledger_app(app_engine, clock, settings)
        simulator_app = create_simulator_app(owner, clock, settings)
        journal: Journal = ledger_app.state.journal
        journal.open_account(NewAccount(code=CUSTOMER, type=AccountType.LIABILITY, currency="USD"))
        try:
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
                    reports=ledger_app.state.reports,
                    journal=journal,
                    owner=owner,
                )
        finally:
            app_engine.dispose()
            owner.dispose()


def test_each_due_report_is_fetched_and_ingested_once(stack: Stack) -> None:
    stack.pull("plain", 10_000)
    stack.at(FRIDAY_REPORT - timedelta(minutes=1))
    assert stack.reports_for("2026-09-03")[0]["lines"] == []  # Thursday's: published, empty
    assert stack.reports_for("2026-09-04") == []  # not published yet

    stack.at(FRIDAY_REPORT)
    stack.at(FRIDAY_REPORT + timedelta(hours=1))

    [friday] = stack.reports_for("2026-09-04")
    assert (friday["outcome"], friday["detail"], friday["received_at"]) == (
        "ingested",
        "",
        "2026-09-05T00:00:00Z",
    )
    [line] = friday["lines"]
    assert (line["type"], line["idempotency_key"], line["amount"], line["adjustment"]) == (
        "settlement",
        "plain",
        10_000,
        None,
    )
    assert line["settled_at"] == "2026-09-04T12:30:00Z"


def test_missing_wrong_and_late_lines_are_stored_and_change_no_balance(stack: Stack) -> None:
    stack.pull("gone", 1_000, {"id": "f1", "kind": "missing_report_line"})
    stack.pull("off", 2_000, {"id": "f2", "kind": "wrong_report_amount", "by": 250})
    stack.pull("late", 4_000, {"id": "f3", "kind": "settlement_after_cutoff"})
    stack.at(TUESDAY_REPORT)

    [friday] = stack.reports_for("2026-09-04")
    assert [(line["idempotency_key"], line["amount"]) for line in friday["lines"]] == [
        ("off", 2_250)
    ]
    [tuesday] = stack.reports_for("2026-09-08")
    assert [(line["idempotency_key"], line["settled_at"]) for line in tuesday["lines"]] == [
        ("late", "2026-09-04T20:00:00Z")
    ]
    # The Ledger booked each Payment as its Provider Events said, nothing more.
    assert stack.balance(CUSTOMER) == 7_000
    assert stack.balance(BANK_CASH) == 7_000
    assert stack.balance(CLEARING) == 0


def test_a_fee_correction_is_booked_against_the_original_payment(stack: Stack) -> None:
    payment_id = stack.pull(
        "fee",
        10_000,
        {"id": "fee", "kind": "provider_adjustment", "amount": 300, "reason": "fee_correction"},
    )
    stack.at(datetime(2026, 9, 4, 12, 30, tzinfo=UTC))  # settles Friday
    stack.at(TUESDAY_REPORT)  # the adjustment comes 1 Banking Day after Friday's report

    [tuesday] = stack.reports_for("2026-09-08")
    [line] = tuesday["lines"]
    adjustment = line["adjustment"]
    assert (line["type"], line["amount"], line["reason"]) == ("adjustment", -300, "fee_correction")
    assert (adjustment["outcome"], adjustment["payment_id"]) == ("booked", payment_id)
    booked = stack.journal.transaction(adjustment["transaction_id"])
    assert booked.entries[0].actor.model_dump() == {
        "kind": "system_job",
        "name": "settlement-report-ingest",
        "version": "1.0.0",
    }
    assert {e.effective_at for e in booked.entries} == {datetime(2026, 9, 8, 16, 0, tzinfo=UTC)}
    # Mintwharf bears it: out of Bank Cash, into Provider Fees; the Customer is untouched.
    assert stack.balance(FEES) == 300
    assert stack.balance(BANK_CASH) == 9_700
    assert stack.balance(CUSTOMER) == 10_000
    assert stack.balance(CLEARING) == 0
    payment = stack.ledger.get(f"/payments/{payment_id}").json()
    assert [a["outcome"] for a in payment["adjustments"]] == ["booked"]
    # As known before the report was ingested, it hadn't happened.
    before = stack.journal.balances(BANK_CASH, END_OF_TIME, TUESDAY_REPORT - timedelta(seconds=1))
    assert before.posted == 10_000


def test_the_same_report_received_again_does_nothing(stack: Stack) -> None:
    stack.pull(
        "fee",
        10_000,
        {"id": "fee", "kind": "provider_adjustment", "amount": 300, "reason": "fee_correction"},
    )
    stack.at(TUESDAY_REPORT)
    body = stack.simulator.settlement_report("simulator", date(2026, 9, 8))

    again = stack.reports.replay("simulator", date(2026, 9, 8), body, {})

    assert again.duplicate
    assert len(stack.reports_for("2026-09-08")) == 1
    assert stack.balance(FEES) == 300


def test_concurrent_ingests_of_one_report_store_it_once(stack: Stack) -> None:
    stack.pull(
        "fee",
        10_000,
        {"id": "fee", "kind": "provider_adjustment", "amount": 300, "reason": "fee_correction"},
    )
    stack.clock.set(TUESDAY_REPORT)
    stack.simulator.tick()
    stack.simulator.publish_reports()
    stack.payments.make_available()
    body = stack.simulator.settlement_report("simulator", date(2026, 9, 8))
    start = Barrier(4)

    def receive(_: int) -> bool:
        start.wait(timeout=10)
        return stack.reports.replay("simulator", date(2026, 9, 8), body, {}).duplicate

    with ThreadPoolExecutor(4) as pool:
        duplicates = list(pool.map(receive, range(4)))

    assert sorted(duplicates) == [False, True, True, True]
    assert stack.balance(FEES) == 300


def test_a_reissued_report_is_kept_as_a_conflict_and_changes_nothing(stack: Stack) -> None:
    stack.pull("plain", 10_000)
    stack.at(FRIDAY_REPORT)
    body = stack.simulator.settlement_report("simulator", FRIDAY)

    reissued = stack.reports.replay("simulator", FRIDAY, body.replace(b",10000,", b",9000,"), {})
    garbled = stack.reports.replay("simulator", FRIDAY, b"not,a,report\n", {})

    assert (reissued.report.outcome, reissued.report.lines) == (ReportOutcome.CONFLICT, [])
    assert "differs from the report ingested" in reissued.report.detail
    assert garbled.report.outcome is ReportOutcome.MALFORMED
    assert [r["outcome"] for r in stack.reports_for("2026-09-04")] == [
        "ingested",
        "conflict",
        "malformed",
    ]
    assert stack.balance(BANK_CASH) == 10_000


def adjustment_line(line_id: str, **changes: str) -> bytes:
    fields = {
        "provider_payment_id": "simpay_nobody",
        "idempotency_key": "unknown",
        "direction": "pull",
        "amount": "-100",
        "reason": "fee_correction",
        **changes,
    }
    return (
        f"2026-09-10,simulator,{line_id},adjustment,{fields['provider_payment_id']},"
        f"{fields['idempotency_key']},ach,{fields['direction']},USD,{fields['amount']},"
        f"2026-09-10T16:00:00Z,l0,{fields['reason']}\n"
    ).encode()


UNMATCHABLE = {
    "no such Payment": ({}, "no Payment simpay_nobody"),
    "another key": ({"idempotency_key": "other"}, "the line describes"),
    "a fee correction paying in": ({"amount": "100"}, "takes money out"),
    "a pull recalled": ({"amount": "100", "reason": "payout_recall"}, "back in"),
    "too much": ({"amount": "-10001"}, "more than the 10000 settled"),
}


def test_an_adjustment_the_ledger_cannot_match_is_stored_not_booked(stack: Stack) -> None:
    payment_id = stack.pull("plain", 10_000)
    stack.at(WEDNESDAY_REPORT - timedelta(hours=1))
    ours = {
        "provider_payment_id": stack.payments.payment(payment_id).provider_payment_id,
        "idempotency_key": "plain",
    }
    header = stack.simulator.settlement_report("simulator", FRIDAY).split(b"\n")[0] + b"\n"
    body = header + b"".join(
        adjustment_line(f"adj-{n}", **({} if not changes else {**ours, **changes}))
        for n, (changes, _) in enumerate(UNMATCHABLE.values())
    )

    received = stack.reports.replay("simulator", date(2026, 9, 10), body, {})

    assert received.report.outcome is ReportOutcome.INGESTED
    for line, (case, (_, why)) in zip(received.report.lines, UNMATCHABLE.items(), strict=True):
        assert line.adjustment is not None, case
        assert (line.adjustment.outcome, line.adjustment.transaction_id) == ("unmatched", None)
        assert why in line.adjustment.detail, case
    assert stack.balance(BANK_CASH) == 10_000
    reported = stack.payments.payment(payment_id).adjustments
    assert [a.outcome for a in reported] == ["unmatched"] * 4  # all but the unknown Payment's
