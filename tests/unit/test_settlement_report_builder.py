"""Building one Provider's Settlement Report for one Banking Day from what settled (SPEC §6,
ADR 0014), report Faults included. Pure: the same settlements give the same bytes."""

import csv
import io
from datetime import UTC, date, datetime
from typing import Any

import pytest

from railbench.common.settlement_reports import COLUMNS
from railbench.simulator.model import (
    DuplicateReportLine,
    FaultPlan,
    MissingReportLine,
    OrphanAdjustment,
    PlannedAdjustment,
    SettlementAfterCutoff,
    WrongReportAmount,
)
from railbench.simulator.reports import Settled, build_report, content_days

THURSDAY, FRIDAY, TUESDAY, WEDNESDAY = (
    date(2026, 9, 3),
    date(2026, 9, 4),
    date(2026, 9, 8),
    date(2026, 9, 9),
)
FRIDAY_ACH = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)  # 08:30 New York
THURSDAY_RTP = datetime(2026, 9, 3, 21, 0, tzinfo=UTC)  # 17:00 New York, before 18:00


def settled(name: str, *faults: FaultPlan, **changes: Any) -> Settled:
    fields: dict[str, Any] = {
        "id": 1,
        "public_id": f"simpay_{name}",
        "idempotency_key": f"key-{name}",
        "rail": "ach",
        "direction": "pull",
        "currency": "USD",
        "amount": 10_000,
        "settled_at": FRIDAY_ACH,
        "faults": faults,
        **changes,
    }
    return Settled(**fields)


def rows(body: bytes) -> list[dict[str, str]]:
    reader = csv.reader(io.StringIO(body.decode()))
    header = next(reader)
    assert tuple(header) == COLUMNS
    return [dict(zip(header, row, strict=True)) for row in reader]


def test_a_report_lists_what_settled_that_banking_day() -> None:
    report = build_report(
        "simulator",
        FRIDAY,
        [
            settled("pull"),
            settled("payout", rail="rtp", direction="push", amount=2_700, settled_at=THURSDAY_RTP),
            settled("later", settled_at=datetime(2026, 9, 8, 12, 30, tzinfo=UTC)),
        ],
    )

    [line] = rows(report.body)
    assert line == {
        "report_date": "2026-09-04",
        "provider": "simulator",
        "line_id": line["line_id"],
        "type": "settlement",
        "provider_payment_id": "simpay_pull",
        "idempotency_key": "key-pull",
        "rail": "ach",
        "direction": "pull",
        "currency": "USD",
        "amount": "10000",
        "settled_at": "2026-09-04T12:30:00Z",
        "original_line_id": "",
        "reason": "",
    }
    assert line["line_id"].startswith("simline_")
    assert report.injected == ()


def test_a_payout_reads_as_money_out() -> None:
    report = build_report(
        "simulator",
        THURSDAY,
        [settled("payout", rail="rtp", direction="push", amount=2_700, settled_at=THURSDAY_RTP)],
    )

    assert [line["amount"] for line in rows(report.body)] == ["-2700"]


def test_a_banking_day_with_nothing_settled_still_has_a_report() -> None:
    report = build_report("simulator", WEDNESDAY, [settled("pull")])

    assert report.body == (",".join(COLUMNS) + "\n").encode()


def test_lines_come_in_settlement_order_and_the_bytes_repeat() -> None:
    early = settled("b", settled_at=datetime(2026, 9, 4, 12, 0, tzinfo=UTC))
    late = settled("a", settled_at=datetime(2026, 9, 4, 13, 0, tzinfo=UTC))

    first = build_report("simulator", FRIDAY, [late, early])
    again = build_report("simulator", FRIDAY, [early, late])

    assert [line["provider_payment_id"] for line in rows(first.body)] == ["simpay_b", "simpay_a"]
    assert first.body == again.body


def test_a_missing_report_line_leaves_the_settlement_off_its_report() -> None:
    fault = MissingReportLine(id="gone")
    report = build_report("simulator", FRIDAY, [settled("pull", fault), settled("other")])

    assert [line["provider_payment_id"] for line in rows(report.body)] == ["simpay_other"]
    [injected] = report.injected
    assert (injected.fault, injected.cause_code) == (fault, "missing_report_line")
    assert injected.params == {"report_date": "2026-09-04"}
    assert "simpay_pull" in injected.root_cause


def test_a_duplicate_report_line_lists_the_settlement_twice_under_two_line_ids() -> None:
    fault = DuplicateReportLine(id="twice")
    report = build_report("simulator", FRIDAY, [settled("pull", fault)])

    listed = rows(report.body)
    assert [(line["provider_payment_id"], line["amount"]) for line in listed] == [
        ("simpay_pull", "10000"),
        ("simpay_pull", "10000"),
    ]
    assert len({line["line_id"] for line in listed}) == 2
    [injected] = report.injected
    assert injected.cause_code == "duplicate_report_line"
    assert injected.line_id in {line["line_id"] for line in listed}
    assert injected.line_id in injected.root_cause
    assert build_report("simulator", TUESDAY, [settled("pull", fault)]).injected == ()


def test_a_wrong_report_amount_reports_the_amount_off_by_its_delta() -> None:
    fault = WrongReportAmount(id="off", by=-250)
    report = build_report("simulator", FRIDAY, [settled("pull", fault)])

    assert [line["amount"] for line in rows(report.body)] == ["9750"]
    [injected] = report.injected
    assert injected.cause_code == "wrong_report_amount"
    assert injected.params == {"by": -250, "report_date": "2026-09-04"}


def test_a_settlement_after_cutoff_lands_on_the_next_banking_days_report() -> None:
    # Friday's ACH settlement booked at the 16:00 Cutoff, on the eve of Labor Day.
    payment = settled("pull", SettlementAfterCutoff(id="late"))

    friday = build_report("simulator", FRIDAY, [payment])
    tuesday = build_report("simulator", TUESDAY, [payment])

    assert rows(friday.body) == [] and friday.injected == ()
    [line] = rows(tuesday.body)
    assert (line["report_date"], line["settled_at"]) == ("2026-09-08", "2026-09-04T20:00:00Z")
    [injected] = tuesday.injected
    assert injected.cause_code == "settlement_after_cutoff"
    assert injected.params == {"report_date": "2026-09-08", "banking_day": "2026-09-04"}
    assert content_days("simulator", payment) == {FRIDAY, TUESDAY}


def test_a_provider_adjustment_claws_back_on_a_later_report() -> None:
    fault = PlannedAdjustment(id="fee", amount=300, reason="fee_correction", after=2)
    payment = settled("pull", fault)

    friday = rows(build_report("simulator", FRIDAY, [payment]).body)
    wednesday = build_report("simulator", WEDNESDAY, [payment])

    [original] = friday
    [adjustment] = rows(wednesday.body)
    assert adjustment == {
        **original,
        "report_date": "2026-09-09",
        "line_id": adjustment["line_id"],
        "type": "adjustment",
        "amount": "-300",
        "settled_at": "2026-09-09T16:00:00Z",  # noon New York
        "original_line_id": original["line_id"],
        "reason": "fee_correction",
    }
    assert adjustment["line_id"] != original["line_id"]
    [injected] = wednesday.injected
    assert injected.cause_code == "provider_adjustment"
    assert injected.params == {
        "amount": 300,
        "reason": "fee_correction",
        "after": 2,
        "report_date": "2026-09-09",
    }
    assert content_days("simulator", payment) == {FRIDAY, WEDNESDAY}


def test_a_recalled_part_of_a_payout_comes_back_in() -> None:
    fault = PlannedAdjustment(id="recall", amount=700, reason="payout_recall")
    payout = settled("payout", fault, direction="push", amount=2_700)

    [adjustment] = rows(build_report("simulator", TUESDAY, [payout]).body)

    assert (adjustment["amount"], adjustment["reason"]) == ("700", "payout_recall")


@pytest.mark.parametrize("day", [FRIDAY, TUESDAY])
def test_each_fault_is_injected_on_one_report_only(day: date) -> None:
    payment = settled("pull", WrongReportAmount(id="off", by=1))

    found = build_report("simulator", day, [payment]).injected

    assert len(found) == (1 if day == FRIDAY else 0)


def test_an_orphan_adjustment_names_a_payment_the_provider_never_made() -> None:
    fault = OrphanAdjustment(id="orphan", amount=450)
    payment = settled("pull", fault)

    [adjustment] = rows(build_report("simulator", TUESDAY, [payment]).body)
    [injected] = build_report("simulator", TUESDAY, [payment]).injected

    assert adjustment["provider_payment_id"].startswith("simpay_")
    assert adjustment["provider_payment_id"] != "simpay_pull"
    assert (adjustment["idempotency_key"], adjustment["amount"], adjustment["reason"]) == (
        "",
        "-450",
        "fee_correction",
    )
    assert adjustment["original_line_id"].startswith("simline_")
    assert (injected.cause_code, injected.line_id) == ("orphan_adjustment", adjustment["line_id"])
    assert content_days("simulator", payment) == {FRIDAY, TUESDAY}
