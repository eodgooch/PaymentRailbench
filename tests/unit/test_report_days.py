"""Which Banking Day's Settlement Report a settlement counts toward (SPEC §6, ADR 0014): the
Banking Day it settled on, if that's a Banking Day and it settled before its Provider's
Cutoff for the Rail, otherwise the next Banking Day."""

from datetime import UTC, date, datetime, timedelta

import pytest

from railbench.common.settlement_reports import cutoff, report_day, report_published_at

FRIDAY = date(2026, 9, 4)  # Monday 7 September is Labor Day
TUESDAY = date(2026, 9, 8)
FRIDAY_ACH_CUTOFF = datetime(2026, 9, 4, 20, 0, tzinfo=UTC)  # 16:00 New York (EDT)
FRIDAY_RTP_CUTOFF = datetime(2026, 9, 4, 22, 0, tzinfo=UTC)  # 18:00 New York


@pytest.mark.parametrize(
    ("rail", "settled_at", "expected"),
    [
        pytest.param("ach", FRIDAY_ACH_CUTOFF - timedelta(seconds=1), FRIDAY, id="ach before"),
        pytest.param("ach", FRIDAY_ACH_CUTOFF, TUESDAY, id="ach at Cutoff, eve of a holiday"),
        pytest.param("ach", FRIDAY_ACH_CUTOFF + timedelta(minutes=1), TUESDAY, id="ach after"),
        pytest.param("rtp", FRIDAY_ACH_CUTOFF, FRIDAY, id="rtp at the ACH Cutoff"),
        pytest.param("rtp", FRIDAY_RTP_CUTOFF - timedelta(seconds=1), FRIDAY, id="rtp before"),
        pytest.param("rtp", FRIDAY_RTP_CUTOFF, TUESDAY, id="rtp at its Cutoff"),
        pytest.param(
            "rtp", datetime(2026, 9, 5, 15, 0, tzinfo=UTC), TUESDAY, id="rtp on a Saturday"
        ),
        pytest.param(
            "rtp", datetime(2026, 9, 7, 3, 59, tzinfo=UTC), TUESDAY, id="rtp late on a Sunday"
        ),
        pytest.param(
            "rtp", datetime(2026, 9, 7, 13, 0, tzinfo=UTC), TUESDAY, id="rtp on Labor Day"
        ),
    ],
)
def test_a_settlement_counts_toward_its_banking_day_before_cutoff(
    rail: str, settled_at: datetime, expected: date
) -> None:
    assert report_day("simulator", rail, settled_at) == expected


def test_each_rail_of_a_provider_has_its_own_cutoff() -> None:
    assert (cutoff("simulator", "ach").hour, cutoff("simulator", "rtp").hour) == (16, 18)


def test_a_report_is_published_after_the_last_cutoff_of_its_day() -> None:
    assert report_published_at(FRIDAY) == datetime(2026, 9, 5, 0, 0, tzinfo=UTC)  # 20:00 EDT


def test_an_unknown_provider_or_rail_has_no_cutoff() -> None:
    with pytest.raises(KeyError):
        cutoff("elsewhere", "ach")
    with pytest.raises(KeyError):
        cutoff("simulator", "wire")
