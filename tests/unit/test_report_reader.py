"""The Provider adapter reads a Settlement Report strictly (ADR 0014): what the Provider
stated, structured, or a MalformedReport, never a guess."""

from datetime import UTC, date, datetime

import pytest

from railbench.common.settlement_reports import AdjustmentReason
from railbench.payments.provider import MalformedReport, ReportLine, read_report_csv

HEADER = (
    "report_date,provider,line_id,type,provider_payment_id,idempotency_key,rail,direction,"
    "currency,amount,settled_at,original_line_id,reason\n"
)
SETTLEMENT = (
    "2026-09-04,simulator,l1,settlement,simpay_1,key-1,ach,pull,USD,10000,2026-09-04T12:30:00Z,,\n"
)
ADJUSTMENT = (
    "2026-09-04,simulator,l2,adjustment,simpay_0,key-0,rtp,push,USD,700,"
    "2026-09-04T16:00:00Z,l0,payout_recall\n"
)
FRIDAY = date(2026, 9, 4)


def read(body: str) -> tuple[ReportLine, ...]:
    return read_report_csv("simulator", FRIDAY, body.encode()).lines


def test_a_report_reads_as_its_lines() -> None:
    assert read(HEADER + SETTLEMENT + ADJUSTMENT) == (
        ReportLine(
            line_id="l1",
            type="settlement",
            provider_payment_id="simpay_1",
            idempotency_key="key-1",
            rail="ach",
            direction="pull",
            currency="USD",
            amount=10_000,
            settled_at=datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
        ),
        ReportLine(
            line_id="l2",
            type="adjustment",
            provider_payment_id="simpay_0",
            idempotency_key="key-0",
            rail="rtp",
            direction="push",
            currency="USD",
            amount=700,
            settled_at=datetime(2026, 9, 4, 16, 0, tzinfo=UTC),
            original_line_id="l0",
            reason=AdjustmentReason.PAYOUT_RECALL,
        ),
    )


def test_an_empty_report_has_no_lines() -> None:
    assert read(HEADER) == ()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty body"),
        pytest.param(HEADER.replace("amount", "value") + SETTLEMENT, id="another header"),
        pytest.param(HEADER + SETTLEMENT.replace("2026-09-04,", "2026-09-03,", 1), id="other day"),
        pytest.param(HEADER + SETTLEMENT.replace("simulator", "elsewhere"), id="other Provider"),
        pytest.param(HEADER + SETTLEMENT.replace("10000", "100.00"), id="a decimal amount"),
        pytest.param(HEADER + SETTLEMENT.replace("10000", "0"), id="a zero amount"),
        pytest.param(HEADER + SETTLEMENT.replace("12:30:00Z", "12:30:00"), id="no offset"),
        pytest.param(HEADER + SETTLEMENT.replace(",ach,", ",wire,"), id="an unknown Rail"),
        pytest.param(HEADER + SETTLEMENT.replace("settlement", "refund"), id="unknown type"),
        pytest.param(HEADER + SETTLEMENT.replace(",,\n", ",l0,\n"), id="settlement naming one"),
        pytest.param(HEADER + ADJUSTMENT.replace("payout_recall", "goodwill"), id="unknown reason"),
        pytest.param(HEADER + ADJUSTMENT.replace(",l0,", ",,"), id="adjustment of nothing"),
        pytest.param(HEADER + SETTLEMENT + SETTLEMENT, id="a line ID twice"),
        pytest.param(HEADER + SETTLEMENT.replace(",,\n", ",\n"), id="a short row"),
    ],
)
def test_anything_off_is_a_malformed_report(body: str) -> None:
    with pytest.raises(MalformedReport):
        read(body)


def test_bytes_that_are_not_text_are_a_malformed_report() -> None:
    with pytest.raises(MalformedReport):
        read_report_csv("simulator", FRIDAY, b"\xff\xfe")
