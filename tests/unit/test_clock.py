from datetime import UTC, datetime, timedelta, timezone

import pytest

from railbench.common.clock import Clock, ManualClock, WallClock


def test_wall_clock_returns_timezone_aware_utc() -> None:
    now = WallClock().now()

    assert now.tzinfo is UTC


def test_wall_clock_moves_forward() -> None:
    clock = WallClock()

    assert clock.now() <= clock.now()


def test_manual_clock_stays_where_it_was_set() -> None:
    clock = ManualClock(datetime(2026, 3, 2, 14, 30, tzinfo=UTC))

    assert clock.now() == datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    assert clock.now() == datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def test_manual_clock_advances() -> None:
    clock = ManualClock(datetime(2026, 3, 2, 23, 0, tzinfo=UTC))

    clock.advance(timedelta(hours=2))

    assert clock.now() == datetime(2026, 3, 3, 1, 0, tzinfo=UTC)


def test_manual_clock_can_be_set() -> None:
    clock = ManualClock(datetime(2026, 3, 2, tzinfo=UTC))

    clock.set(datetime(2026, 7, 4, 9, 0, tzinfo=UTC))

    assert clock.now() == datetime(2026, 7, 4, 9, 0, tzinfo=UTC)


def test_manual_clock_normalises_other_offsets_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    clock = ManualClock(datetime(2026, 3, 2, 9, 0, tzinfo=eastern))

    assert clock.now() == datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
    assert clock.now().tzinfo is UTC


def test_manual_clock_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(datetime(2026, 3, 2))  # noqa: DTZ001

    clock = ManualClock(datetime(2026, 3, 2, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2026, 3, 3))  # noqa: DTZ001


def test_manual_clock_refuses_to_go_backwards() -> None:
    clock = ManualClock(datetime(2026, 3, 2, tzinfo=UTC))

    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))


def test_both_clocks_satisfy_the_clock_protocol() -> None:
    clocks: list[Clock] = [WallClock(), ManualClock(datetime(2026, 3, 2, tzinfo=UTC))]

    assert all(isinstance(c.now(), datetime) for c in clocks)
