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
Return can come at the moment of settlement, and at the deadline itself. A Late Return is one
after the 2-Banking-Day deadline, when the funds are (or would be) Available.

`NachaReturnCode` is the type every model carries a return code as: a known `ReturnCode`,
read from and written as its name (`"R10"`).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema

from railbench.common.calendar import (
    add_banking_days,
    banking_day_starts,
    new_york_date,
    next_banking_day,
)

STANDARD_BANKING_DAYS = 2
EXTENDED_CALENDAR_DAYS = 60


class UnknownReturnCode(ValueError):
    """A return code the Simulator and the Ledger don't know."""


class WindowKind(StrEnum):
    BANKING_DAYS = "banking_days"
    EXTENDED = "extended"

    @property
    def label(self) -> str:
        if self is WindowKind.BANKING_DAYS:
            return "to the opening of the 2nd Banking Day after settlement"
        return "to the opening of the Banking Day after the 60th calendar day after settlement"


@dataclass(frozen=True)
class ReturnCode:
    code: str
    reason: str
    window: WindowKind

    def deadline(self, settled_at: datetime) -> datetime:
        """The last moment this Return can arrive for an Entry settled at `settled_at`."""
        if self.window is WindowKind.BANKING_DAYS:
            return _standard_deadline(settled_at)
        sixtieth = new_york_date(settled_at) + timedelta(days=EXTENDED_CALENDAR_DAYS)
        return banking_day_starts(next_banking_day(sixtieth))

    def within_window(self, settled_at: datetime, returned_at: datetime) -> bool:
        return settled_at <= returned_at <= self.deadline(settled_at)


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


def return_code(name: str) -> ReturnCode:
    """The known return code called `name`. Raises UnknownReturnCode."""
    found = RETURN_CODES.get(name)
    if found is None:
        raise UnknownReturnCode(f"no NACHA return code {name!r} is known")
    return found


def _parse(value: Any) -> ReturnCode:
    if isinstance(value, ReturnCode):
        return value
    if not isinstance(value, str):
        raise UnknownReturnCode(f"a return code is a name such as 'R10', not {value!r}")
    return return_code(value)


NachaReturnCode = Annotated[
    ReturnCode,
    BeforeValidator(_parse),
    PlainSerializer(lambda code: code.code, return_type=str),
    WithJsonSchema({"type": "string", "examples": ["R10"]}),
]


def return_deadline(code: str, settled_at: datetime) -> datetime:
    """The last moment a Return with `code` can arrive for an Entry settled at `settled_at`.
    Raises UnknownReturnCode."""
    return return_code(code).deadline(settled_at)


def within_return_window(code: str, settled_at: datetime, returned_at: datetime) -> bool:
    """Whether a Return with `code` at `returned_at` is inside its Return Window."""
    return return_code(code).within_window(settled_at, returned_at)


def is_late(settled_at: datetime, returned_at: datetime) -> bool:
    """Whether a Return at `returned_at` comes after the 2-Banking-Day window has closed,
    the moment ACH funds settled at `settled_at` become Available (ADR 0004): a Late
    Return. Decided by time alone, never by whether the availability job has run yet."""
    return returned_at > _standard_deadline(settled_at)


def _standard_deadline(settled_at: datetime) -> datetime:
    return banking_day_starts(add_banking_days(new_york_date(settled_at), STANDARD_BANKING_DAYS))
