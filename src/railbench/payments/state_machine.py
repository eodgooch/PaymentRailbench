"""The Payment state machines (SPEC §4.1, §4.2), as a pure decision.

ACH, a pull or a push:

    created → submitted → settled → available (a pull only; the Ledger decides it)
    created | submitted → canceled | declined
    settled | available → returned (a pull only: a Return, with its NACHA return code)

RTP, a credit push only, instant and final:

    created → settled | declined

Both only move forward. An event reporting a state the Payment has reached or passed is
stale: recorded, and nothing happens. A canceled or declined Payment has passed the states
it was recorded in on the way, so a second `submitted` for a Payment that was submitted and
then declined is stale; but a `submitted` for one that went straight from created to
canceled or declined is impossible (the Provider may really have sent the debit). An event
that skips ahead along the main path (a `settled` for a Payment still `created`) advances
through every state it skipped, so the Ledger books each step. Anything else (a decline
after settlement, a settlement after a decline) is impossible: the caller records an
exception and writes nothing to the Ledger.

A Return is final, and can only follow a settlement the Ledger has recorded: a Return for a
Payment still created or submitted is impossible (SPEC §4.2's own example), even if only the
settlement is late, and so is one after a decline or cancellation. After a Return, every
state on the main path counts as passed (a late settlement or a second Return is stale),
and a decline or cancellation is impossible. Only a pull is returned: a Return of a push (a
Payout) is impossible.

Nothing follows an RTP ending: a settled RTP Payment can't be declined or returned, and RTP
has no submission or cancellation step, so any other RTP event is impossible.
"""

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

Rail = Literal["ach", "rtp"]
Direction = Literal["pull", "push"]


class PaymentState(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    SETTLED = "settled"
    AVAILABLE = "available"
    CANCELED = "canceled"
    DECLINED = "declined"
    RETURNED = "returned"


MAIN_PATH = (
    PaymentState.CREATED,
    PaymentState.SUBMITTED,
    PaymentState.SETTLED,
    PaymentState.AVAILABLE,
)
ENDS_BEFORE_SETTLEMENT = (PaymentState.CANCELED, PaymentState.DECLINED)
RETURNABLE = (PaymentState.SETTLED, PaymentState.AVAILABLE)
RTP_ENDINGS = (PaymentState.SETTLED, PaymentState.DECLINED)


@dataclass(frozen=True)
class Advance:
    """Move through `path`, in order, booking each state."""

    path: tuple[PaymentState, ...]


@dataclass(frozen=True)
class Stale:
    """The Payment has already reached or passed this state."""


@dataclass(frozen=True)
class Impossible:
    reason: str


Decision = Advance | Stale | Impossible


def decide(
    current: PaymentState,
    reported: PaymentState,
    reached: Collection[PaymentState] = (),
    *,
    rail: Rail,
    direction: Direction,
) -> Decision:
    """What an event reporting `reported` does to a `direction` Payment on `rail` now at
    `current`, having been recorded in the states `reached` (its history, `current` included
    or not)."""
    if rail == "rtp":
        return _decide_rtp(current, reported)

    def impossible() -> Impossible:
        return Impossible(f"an ACH Payment can't go from {current} to {reported}")

    if current in ENDS_BEFORE_SETTLEMENT:
        passed = {current, PaymentState.CREATED, *reached}
        return Stale() if reported in passed else impossible()
    if current is PaymentState.RETURNED:
        return Stale() if reported in (*MAIN_PATH, PaymentState.RETURNED) else impossible()
    if reported is PaymentState.RETURNED:
        if direction == "push":
            return Impossible("an ACH push (a Payout) can't be returned here: only pulls are")
        if current in RETURNABLE:
            return Advance(path=(PaymentState.RETURNED,))
        return Impossible(f"an ACH Payment can't go from {current} to returned: it never settled")
    position = MAIN_PATH.index(current)
    if reported in ENDS_BEFORE_SETTLEMENT:
        return Advance(path=(reported,)) if position < 2 else impossible()
    target = MAIN_PATH.index(reported)
    if target <= position:
        return Stale()
    if reported is PaymentState.AVAILABLE and current is not PaymentState.SETTLED:
        return impossible()
    return Advance(path=MAIN_PATH[position + 1 : target + 1])


def _decide_rtp(current: PaymentState, reported: PaymentState) -> Decision:
    if reported in (current, PaymentState.CREATED):
        return Stale()
    if current is PaymentState.CREATED and reported in RTP_ENDINGS:
        return Advance(path=(reported,))
    return Impossible(f"an RTP Payment can't go from {current} to {reported}")
