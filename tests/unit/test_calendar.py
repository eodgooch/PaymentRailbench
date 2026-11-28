"""The Banking Day calendar: weekends and Federal Reserve holidays, with the Fed's own
observance rules (checked against federalreserve.gov/aboutthefed/k8.htm, 2026-2030)."""

from datetime import UTC, date, datetime

import pytest

from railbench.common.calendar import (
    add_banking_days,
    banking_day_starts,
    fed_holidays,
    is_banking_day,
    next_banking_day,
)

# The Fed's published schedule, as observed (the days the Reserve Banks are closed).
PUBLISHED = {
    2026: [
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 5, 25),
        date(2026, 6, 19),
        # July 4 2026 is a Saturday: the Reserve Banks open the Friday before.
        date(2026, 9, 7),
        date(2026, 10, 12),
        date(2026, 11, 11),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ],
    2027: [
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 5, 31),
        # June 19 2027 is a Saturday: not observed.
        date(2027, 7, 5),  # July 4 2027 is a Sunday: closed the Monday after.
        date(2027, 9, 6),
        date(2027, 10, 11),
        date(2027, 11, 11),
        date(2027, 11, 25),
        # December 25 2027 is a Saturday: not observed.
    ],
    2029: [
        date(2029, 1, 1),
        date(2029, 1, 15),
        date(2029, 2, 19),
        date(2029, 5, 28),
        date(2029, 6, 19),
        date(2029, 7, 4),
        date(2029, 9, 3),
        date(2029, 10, 8),
        date(2029, 11, 12),  # November 11 2029 is a Sunday.
        date(2029, 11, 22),
        date(2029, 12, 25),
    ],
}


@pytest.mark.parametrize("year", sorted(PUBLISHED))
def test_fed_holidays_match_the_published_schedule(year: int) -> None:
    assert sorted(fed_holidays(year)) == PUBLISHED[year]


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 9, 4), True),  # Friday before Labor Day
        (date(2026, 9, 5), False),  # Saturday
        (date(2026, 9, 6), False),  # Sunday
        (date(2026, 9, 7), False),  # Labor Day
        (date(2026, 9, 8), True),
        (date(2026, 7, 3), True),  # the Fed is open the Friday before a Saturday holiday
        (date(2027, 12, 31), True),  # New Year's Day 2028 is a Saturday
    ],
)
def test_weekends_and_fed_holidays_are_not_banking_days(day: date, expected: bool) -> None:
    assert is_banking_day(day) is expected


def test_the_next_banking_day_skips_a_holiday_weekend() -> None:
    assert next_banking_day(date(2026, 9, 4)) == date(2026, 9, 8)
    assert next_banking_day(date(2026, 9, 5)) == date(2026, 9, 8)


def test_two_banking_days_after_the_friday_before_labor_day_is_wednesday() -> None:
    assert add_banking_days(date(2026, 9, 4), 1) == date(2026, 9, 8)
    assert add_banking_days(date(2026, 9, 4), 2) == date(2026, 9, 9)


def test_counting_from_a_day_that_is_not_a_banking_day() -> None:
    # From Saturday, the first Banking Day after is Tuesday (Monday is Labor Day).
    assert add_banking_days(date(2026, 9, 5), 1) == date(2026, 9, 8)
    assert add_banking_days(date(2026, 9, 5), 0) == date(2026, 9, 5)


def test_adding_banking_days_spans_a_year_end() -> None:
    # Dec 31 2026 is Thursday; Jan 1 2027 a holiday; then a weekend.
    assert add_banking_days(date(2026, 12, 31), 1) == date(2027, 1, 4)


def test_add_banking_days_refuses_a_negative_count() -> None:
    with pytest.raises(ValueError):
        add_banking_days(date(2026, 9, 4), -1)


def test_a_banking_day_starts_at_midnight_in_new_york() -> None:
    # Eastern Daylight Time in September (UTC-4), Eastern Standard Time in January (UTC-5).
    assert banking_day_starts(date(2026, 9, 9)) == datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
    assert banking_day_starts(date(2027, 1, 4)) == datetime(2027, 1, 4, 5, 0, tzinfo=UTC)
