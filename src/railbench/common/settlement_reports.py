"""The Settlement Report contract both sides share (SPEC §6, ADR 0014), the way a Provider's
published documentation would be: each Provider's Cutoff per Rail, which Banking Day's
report a settlement counts toward, when a report is published, and the CSV's columns.

A settlement counts toward the Banking Day it happened on (in New York) if that is a Banking
Day and it happened before the Provider's Cutoff for its Rail; at or after the Cutoff, or on
a weekend or Fed holiday, it counts toward the next Banking Day. RTP settles 24/7/365, so a
weekend or holiday RTP settlement lands on the next Banking Day's report. Each Banking Day's
report is published at 20:00 New York, after every Rail's Cutoff that day.

One report per Provider per Banking Day, as CSV: a header row of COLUMNS, then one row per
line, `\\n`-terminated. `amount` is signed integer Minor Units of money into (+) or out of
(-) Mintwharf's account at the Provider: a settled pull is +, a settled push (a Payout) is
-, and a Provider Adjustment is the change it makes (a fee correction -, a recalled part of
a Payout +). `settled_at` is when the Provider says the money moved, as a UTC instant.

This module imports nothing from either service.
"""

from datetime import date, datetime, time

from railbench.common.calendar import at_new_york, is_banking_day, new_york_date, next_banking_day

# Each Provider's Cutoff per Rail, New York time (fictional, documented in ADR 0014). The
# Simulator plays the one Provider today; ACH's Cutoff is also its submission Cutoff.
CUTOFFS: dict[str, dict[str, time]] = {
    "simulator": {"ach": time(16, 0), "rtp": time(18, 0)},
}
PUBLISHED_AT = time(20, 0)

COLUMNS = (
    "report_date",
    "provider",
    "line_id",
    "type",
    "provider_payment_id",
    "idempotency_key",
    "rail",
    "direction",
    "currency",
    "amount",
    "settled_at",
    "original_line_id",
    "reason",
)
LINE_TYPES = ("settlement", "adjustment")
ADJUSTMENT_REASONS = ("fee_correction", "payout_recall")


def cutoff(provider: str, rail: str) -> time:
    """The Provider's Cutoff for the Rail. Raises KeyError for one it doesn't publish."""
    return CUTOFFS[provider][rail]


def report_day(provider: str, rail: str, settled_at: datetime) -> date:
    """The Banking Day whose Settlement Report lists a settlement at `settled_at`."""
    day = new_york_date(settled_at)
    if not is_banking_day(day) or settled_at >= at_new_york(day, cutoff(provider, rail)):
        day = next_banking_day(day)
    return day


def report_published_at(day: date) -> datetime:
    """When the Settlement Report for Banking Day `day` is published."""
    return at_new_york(day, PUBLISHED_AT)


def report_filename(provider: str, day: date) -> str:
    return f"{provider}-{day.isoformat()}.csv"
