"""The Banking Day calendar: the days the Federal Reserve is open (ADR 0010).

A Banking Day is a weekday that isn't a Federal Reserve holiday. The holidays and their
observance follow the Fed's published schedule (federalreserve.gov/aboutthefed/k8.htm):

- New Year's Day (Jan 1), Birthday of Martin Luther King, Jr. (3rd Monday in January),
  Washington's Birthday (3rd Monday in February), Memorial Day (last Monday in May),
  Juneteenth National Independence Day (Jun 19), Independence Day (Jul 4), Labor Day
  (1st Monday in September), Columbus Day (2nd Monday in October), Veterans Day (Nov 11),
  Thanksgiving Day (4th Thursday in November) and Christmas Day (Dec 25).
- A holiday on a Sunday is observed the Monday after.
- A holiday on a Saturday is not observed: the Reserve Banks open the Friday before.

Banking Days are dates in New York time, where the Fed keeps its hours.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")

_MONDAY, _THURSDAY, _SATURDAY, _SUNDAY = 0, 3, 5, 6


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    following = date(year + month // 12, month % 12 + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


@cache
def fed_holidays(year: int) -> frozenset[date]:
    """The days in `year` the Federal Reserve Banks are closed for a holiday, as observed."""
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 11, 11)]
    fixed.append(date(year, 12, 25))
    observed = set()
    for holiday in fixed:
        if holiday.weekday() == _SUNDAY:
            observed.add(holiday + timedelta(days=1))
        elif holiday.weekday() != _SATURDAY:
            observed.add(holiday)
    observed |= {
        _nth_weekday(year, 1, _MONDAY, 3),
        _nth_weekday(year, 2, _MONDAY, 3),
        _last_weekday(year, 5, _MONDAY),
        _nth_weekday(year, 9, _MONDAY, 1),
        _nth_weekday(year, 10, _MONDAY, 2),
        _nth_weekday(year, 11, _THURSDAY, 4),
    }
    return frozenset(observed)


def is_banking_day(day: date) -> bool:
    return day.weekday() < _SATURDAY and day not in fed_holidays(day.year)


def next_banking_day(day: date) -> date:
    """The first Banking Day strictly after `day`."""
    day += timedelta(days=1)
    while not is_banking_day(day):
        day += timedelta(days=1)
    return day


def banking_day_on_or_after(day: date) -> date:
    """`day` if it's a Banking Day, otherwise the next one."""
    return day if is_banking_day(day) else next_banking_day(day)


def banking_days_from(day: date, until: date | None = None) -> Iterator[date]:
    """Every Banking Day from `day` on, in order: to `until` inclusive, or without end."""
    day = banking_day_on_or_after(day)
    while until is None or day <= until:
        yield day
        day = next_banking_day(day)


def add_banking_days(day: date, count: int) -> date:
    """The `count`th Banking Day after `day` (`day` itself for 0, whether or not it is one)."""
    if count < 0:
        raise ValueError("count Banking Days forwards only")
    for _ in range(count):
        day = next_banking_day(day)
    return day


def new_york_date(moment: datetime) -> date:
    """The calendar date of `moment` in New York."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("a moment must be timezone-aware")
    return moment.astimezone(NEW_YORK).date()


def at_new_york(day: date, clock_time: time) -> datetime:
    """`clock_time` in New York on `day`, as a UTC datetime."""
    return datetime.combine(day, clock_time, NEW_YORK).astimezone(UTC)


def banking_day_starts(day: date) -> datetime:
    """Midnight in New York at the start of `day`, in UTC."""
    return at_new_york(day, time(0, 0))
