"""NACHA return codes and their Return Windows (SPEC §4.3, ADR 0013), measured on the Banking
Day calendar from the Settlement Date of the original Entry.

A Friday settlement before Labor Day: its 2-Banking-Day window runs over the weekend and the
holiday to the opening of Wednesday 9 September; its 60-day window to the opening of the
Banking Day after Tuesday 3 November (the 60th calendar day)."""

from datetime import UTC, datetime, timedelta

import pytest

from railbench.common.returns import (
    RETURN_CODES,
    UnknownReturnCode,
    WindowKind,
    return_deadline,
    within_return_window,
)

FRIDAY_SETTLEMENT = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)  # 08:30 New York
WEDNESDAY_OPENING = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)  # midnight New York (EDT)
NOVEMBER_4_OPENING = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)  # midnight New York (EST)
SECOND = timedelta(seconds=1)


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("R01", WindowKind.BANKING_DAYS),
        ("R02", WindowKind.BANKING_DAYS),
        ("R03", WindowKind.BANKING_DAYS),
        ("R10", WindowKind.EXTENDED),
        ("R05", WindowKind.EXTENDED),
        ("R07", WindowKind.EXTENDED),
        ("R11", WindowKind.EXTENDED),
        ("R29", WindowKind.BANKING_DAYS),  # unauthorized, but to a non-consumer account
    ],
)
def test_each_code_has_its_window(code: str, kind: WindowKind) -> None:
    assert RETURN_CODES[code].window is kind


@pytest.mark.parametrize("code", ["R01", "R02", "R03"])
def test_an_administrative_return_is_due_by_the_second_banking_day_after_settlement(
    code: str,
) -> None:
    assert return_deadline(code, FRIDAY_SETTLEMENT) == WEDNESDAY_OPENING
    assert within_return_window(code, FRIDAY_SETTLEMENT, WEDNESDAY_OPENING)
    assert not within_return_window(code, FRIDAY_SETTLEMENT, WEDNESDAY_OPENING + SECOND)


def test_an_unauthorized_consumer_return_has_60_calendar_days_and_then_the_next_opening() -> None:
    assert return_deadline("R10", FRIDAY_SETTLEMENT) == NOVEMBER_4_OPENING
    assert within_return_window("R10", FRIDAY_SETTLEMENT, NOVEMBER_4_OPENING)
    assert not within_return_window("R10", FRIDAY_SETTLEMENT, NOVEMBER_4_OPENING + SECOND)


def test_the_extended_deadline_skips_a_holiday_after_the_60th_day() -> None:
    # Settled Monday 26 October 2026: the 60th day is Friday 25 December (Christmas, so the
    # 26th-27th are a weekend), and the next Banking Day is Monday 28 December.
    settled = datetime(2026, 10, 26, 12, 30, tzinfo=UTC)
    assert return_deadline("R10", settled) == datetime(2026, 12, 28, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize("code", ["R01", "R10"])
def test_a_return_cannot_come_before_the_settlement(code: str) -> None:
    assert within_return_window(code, FRIDAY_SETTLEMENT, FRIDAY_SETTLEMENT)
    assert not within_return_window(code, FRIDAY_SETTLEMENT, FRIDAY_SETTLEMENT - SECOND)


def test_an_unknown_code_has_no_window() -> None:
    with pytest.raises(UnknownReturnCode):
        return_deadline("R99", FRIDAY_SETTLEMENT)
