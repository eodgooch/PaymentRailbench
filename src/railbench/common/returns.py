"""NACHA return codes and their Return Windows (SPEC §4.3, ADR 0013).

A Return must reach the ODFI by a deadline measured from the Settlement Date of the original
Entry (the New York date it settled on), on the Banking Day calendar:

- most codes, including R01 (insufficient funds), R02 (account closed) and R03 (no account,
  unable to locate): by the opening of business on the second Banking Day following the
  Settlement Date;
- unauthorized debits to a consumer account (R05, R07, R10, R11): the extended window, by
  the opening of business on the Banking Day following the 60th calendar day after the
  Settlement Date.

"Opening of business" is modelled as midnight New York time at the start of that Banking Day,
the same moment ACH funds become Available (ADR 0004, ADR 0010). Both bounds are inclusive: a
Return can come at the moment of settlement, and at the deadline itself. Checked against
nacha.org on 2026-11-27 (see ADR 0013).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from railbench.common.calendar import (
    add_banking_days,
    banking_day_starts,
    new_york_date,
    next_banking_day,
)

STANDARD_BANKING_DAYS = 2
EXTENDED_CALENDAR_DAYS = 60


class WindowKind(StrEnum):
    BANKING_DAYS = "banking_days"  # by the opening of the 2nd Banking Day after settlement
    EXTENDED = "extended"  # by the opening of the Banking Day after the 60th calendar day


@dataclass(frozen=True)
class ReturnCode:
    code: str
    reason: str
    window: WindowKind


RETURN_CODES: dict[str, ReturnCode] = {
    code.code: code
    for code in (
        ReturnCode("R01", "Insufficient funds", WindowKind.BANKING_DAYS),
        ReturnCode("R02", "Account closed", WindowKind.BANKING_DAYS),
        ReturnCode("R03", "No account, or unable to locate the account", WindowKind.BANKING_DAYS),
        ReturnCode("R04", "Invalid account number structure", WindowKind.BANKING_DAYS),
        ReturnCode(
            "R05",
            "Unauthorized debit to a consumer account using a corporate SEC code",
            WindowKind.EXTENDED,
        ),
        ReturnCode("R07", "Authorization revoked by the customer", WindowKind.EXTENDED),
        ReturnCode(
            "R10",
            "Customer advises the Originator is not known to them, or not authorized to "
            "debit their account",
            WindowKind.EXTENDED,
        ),
        ReturnCode(
            "R11",
            "Customer advises the entry is not in accordance with the terms of the authorization",
            WindowKind.EXTENDED,
        ),
        ReturnCode(
            "R29",
            "Corporate customer advises the entry is not authorized",
            WindowKind.BANKING_DAYS,
        ),
    )
}


def return_deadline(code: str, settled_at: datetime) -> datetime:
    """The last moment a Return with `code` can arrive for an Entry settled at `settled_at`.
    Raises KeyError for a code the Simulator doesn't know."""
    window = RETURN_CODES[code].window
    settlement_date = new_york_date(settled_at)
    if window is WindowKind.BANKING_DAYS:
        return banking_day_starts(add_banking_days(settlement_date, STANDARD_BANKING_DAYS))
    sixtieth = settlement_date + timedelta(days=EXTENDED_CALENDAR_DAYS)
    return banking_day_starts(next_banking_day(sixtieth))


def is_late(settled_at: datetime, returned_at: datetime) -> bool:
    """Whether a Return at `returned_at` comes after the 2-Banking-Day window has closed,
    when the Ledger has made the funds Available (ADR 0004): a Late Return."""
    settlement_date = new_york_date(settled_at)
    return returned_at > banking_day_starts(
        add_banking_days(settlement_date, STANDARD_BANKING_DAYS)
    )


def within_return_window(code: str, settled_at: datetime, returned_at: datetime) -> bool:
    """Whether a Return with `code` at `returned_at` is inside its Return Window."""
    return settled_at <= returned_at <= return_deadline(code, settled_at)
