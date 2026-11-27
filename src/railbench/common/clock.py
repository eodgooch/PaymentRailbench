"""The one source of time for the Ledger and the Simulator.

Nothing else reads the system time (ruff's banned-api rule enforces this). Code that needs
"now" takes a `Clock`, so tests and Scenarios can run on simulated time. Every Clock returns
timezone-aware UTC datetimes.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware, in UTC."""
        ...


class WallClock:
    """The real time. The default outside tests and Scenarios."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to, for tests, Scenarios and demos."""

    def __init__(self, start: datetime) -> None:
        self._now = _as_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = _as_utc(moment)

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("a ManualClock can't advance backwards; use set() to rewind")
        self._now += delta


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Clock times must be timezone-aware")
    return moment.astimezone(UTC)
