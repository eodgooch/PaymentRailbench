"""The ACH Payment state machine (SPEC §4.1, §4.2), as a pure decision.

    created → submitted → settled → available
    created | submitted → canceled | declined

It only moves forward. An event reporting a state the Payment has reached or passed is
stale: recorded, and nothing happens. A canceled or declined Payment has passed created and
submitted, so a `submitted` delivered after its decline is stale too. An event that skips
ahead along the main path (a `settled` for a Payment still `created`) advances through every
state it skipped, so the Ledger books each step. Anything else (a decline after settlement,
a settlement after a decline) is impossible: the caller records an exception and writes
nothing to the Ledger.
Returns join in #7.
"""

from dataclasses import dataclass
from enum import StrEnum


class AchState(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    SETTLED = "settled"
    AVAILABLE = "available"
    CANCELED = "canceled"
    DECLINED = "declined"


MAIN_PATH = (AchState.CREATED, AchState.SUBMITTED, AchState.SETTLED, AchState.AVAILABLE)
ENDS_BEFORE_SETTLEMENT = (AchState.CANCELED, AchState.DECLINED)


@dataclass(frozen=True)
class Advance:
    """Move through `path`, in order, booking each state."""

    path: tuple[AchState, ...]


@dataclass(frozen=True)
class Stale:
    """The Payment has already reached or passed this state."""


@dataclass(frozen=True)
class Impossible:
    reason: str


Decision = Advance | Stale | Impossible


def decide(current: AchState, reported: AchState) -> Decision:
    def impossible() -> Impossible:
        return Impossible(f"an ACH Payment can't go from {current} to {reported}")

    if current in ENDS_BEFORE_SETTLEMENT:
        passed = (current, AchState.CREATED, AchState.SUBMITTED)
        return Stale() if reported in passed else impossible()
    position = MAIN_PATH.index(current)
    if reported in ENDS_BEFORE_SETTLEMENT:
        return Advance(path=(reported,)) if position < 2 else impossible()
    target = MAIN_PATH.index(reported)
    if target <= position:
        return Stale()
    if reported is AchState.AVAILABLE and current is not AchState.SETTLED:
        return impossible()
    return Advance(path=MAIN_PATH[position + 1 : target + 1])
