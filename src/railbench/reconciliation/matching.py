"""The matching rules, as a pure function (SPEC §7.1, ADR 0015): the Ledger's view of one
Provider's Banking Day D and the report lines that bear on it in, the Breaks out. No
database, no Clock; the same view gives the same Breaks, in the same order.

The view is what the Ledger knew at one moment (`known_at`, chosen by the caller): every
Payment it had recorded, with the states it had reached; the settlement lines of the
reports for D and for the next Banking Day N (and earlier ones, for duplicates); D's
Provider Adjustment lines, each with the Ledger's latest decision about it; and any
re-issue of D's report. A settled Payment is expected on the report of the Banking Day its
Settlement Effective Time counts toward (`report_day`, the Provider's published Cutoff
rule), and a line names its Payment by the Provider's Payment ID, cross-checked against the
Idempotency Key, Rail, direction and currency the Ledger recorded.

A settled Payment's line is its first agreeing settlement line (by report day, then
position); any other agreeing line for it is a duplicate. A Payment's line is judged on its
expected day's run if it's on a report up to the next Banking Day's, otherwise on the run of
the report it's on, which links the Break it raises to the missing-at-Provider Break the
expected day's run raised (`related_breaks`). One rule, one function, per Break Type:

- missing at Provider: a Payment the Ledger recorded settled, expected on D, with no line on
  any report up to N's;
- timing mismatch: its line is on another report than its expected day's;
- amount mismatch: its line's amount isn't what settled (+ a pull, - a push);
- duplicate: a second settlement line for one Payment (raised on the report it's on), or a
  re-issue of D's report that states other lines than the one ingested (one per currency);
- status mismatch: a settlement line for a Payment the Ledger hadn't recorded settled (still
  in flight, declined or canceled), or a Provider Adjustment still pending;
- missing in Ledger: a settlement line naming no Payment the Ledger has (or one that
  contradicts the Payment it names), or a Provider Adjustment the Ledger left unmatched.

A booked Provider Adjustment agrees with the Ledger: no Break. Exposure is in Minor Units,
never negative: the size of the money in question (the difference, for an amount mismatch).
"""

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from railbench.common.calendar import next_banking_day
from railbench.common.settlement_reports import report_day
from railbench.payments.model import AdjustmentOutcome
from railbench.payments.provider import LineType
from railbench.payments.state_machine import Direction, PaymentState, Rail


class BreakType(StrEnum):
    MISSING_IN_LEDGER = "missing_in_ledger"
    MISSING_AT_PROVIDER = "missing_at_provider"
    AMOUNT_MISMATCH = "amount_mismatch"
    TIMING_MISMATCH = "timing_mismatch"
    DUPLICATE = "duplicate"
    STATUS_MISMATCH = "status_mismatch"


@dataclass(frozen=True, order=True)
class ReportDay:
    """One Provider's Banking Day: what one Settlement Report covers, and what one run of
    Reconciliation reconciles."""

    provider: str
    day: date

    @property
    def next(self) -> "ReportDay":
        """The same Provider's next Banking Day."""
        return ReportDay(self.provider, next_banking_day(self.day))

    def __str__(self) -> str:
        return f"{self.provider}:{self.day.isoformat()}"


def _ids(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted(set(values)))


@dataclass(frozen=True)
class Evidence:
    """The rows a Break rests on, by their ids in the database: Payments, their state
    changes, Inbox events and Ledger Transactions; Settlement Reports, their lines and the
    Provider Adjustment decisions."""

    payments: tuple[int, ...] = ()
    transitions: tuple[int, ...] = ()
    inbox: tuple[int, ...] = ()
    transactions: tuple[int, ...] = ()
    reports: tuple[int, ...] = ()
    report_lines: tuple[int, ...] = ()
    provider_adjustments: tuple[int, ...] = ()

    def __or__(self, other: "Evidence") -> "Evidence":
        return Evidence(
            **{name: _ids((*getattr(self, name), *getattr(other, name))) for name in EVIDENCE_KINDS}
        )


EVIDENCE_KINDS = tuple(Evidence.__dataclass_fields__)
NO_EVIDENCE = Evidence()


@dataclass(frozen=True)
class LedgerPayment:
    """A Payment as the Ledger knew it: its current state and, once settled, when (the
    Effective Time of its `settled` state change)."""

    id: int
    provider_payment_id: str
    idempotency_key: str
    rail: Rail
    direction: Direction
    currency: str
    amount: int
    state: PaymentState
    settled_at: datetime | None
    evidence: Evidence = NO_EVIDENCE

    @property
    def signed_amount(self) -> int:
        """What its settlement line should say: + into Mintwharf's account, - out of it."""
        return self.amount if self.direction == "pull" else -self.amount


@dataclass(frozen=True)
class StatedLine:
    """A settlement or adjustment line as stored from an ingested report."""

    id: int  # the line's row id
    report_id: int
    report_date: date
    position: int
    line_id: str
    type: LineType
    provider_payment_id: str
    idempotency_key: str
    rail: Rail
    direction: Direction
    currency: str
    amount: int
    settled_at: datetime

    @property
    def evidence(self) -> Evidence:
        return Evidence(reports=(self.report_id,), report_lines=(self.id,))

    @property
    def order(self) -> tuple[date, int, int]:
        return (self.report_date, self.position, self.id)

    @property
    def stated(self) -> "RestatedLine":
        return RestatedLine(self.line_id, self.currency, self.amount)


@dataclass(frozen=True)
class AdjustmentLine:
    """A Provider Adjustment line on D's report, with the Ledger's latest decision about it
    (None: no decision recorded yet, which counts as pending)."""

    line: StatedLine
    outcome: AdjustmentOutcome | None
    decisions: tuple[int, ...] = ()
    payment_id: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class RestatedLine:
    line_id: str
    currency: str
    amount: int

    def as_fact(self) -> dict[str, Any]:
        return {"line_id": self.line_id, "currency": self.currency, "amount": self.amount}


@dataclass(frozen=True)
class Reissue:
    """Another version of D's report the Provider sent after one was ingested (kept as a
    `conflict`), with the lines it states."""

    report_id: int
    body_sha256: str
    lines: tuple[RestatedLine, ...]


@dataclass(frozen=True)
class DayView:
    report_day: ReportDay
    report_id: int  # D's ingested report
    payments: Mapping[str, LedgerPayment]  # by the Provider's Payment ID
    settlement_lines: tuple[StatedLine, ...]  # every one bearing on D, up to N's report
    adjustment_lines: tuple[AdjustmentLine, ...] = ()  # on D's report
    reissues: tuple[Reissue, ...] = ()

    @property
    def day(self) -> date:
        return self.report_day.day

    @property
    def provider(self) -> str:
        return self.report_day.provider


@dataclass(frozen=True)
class FoundBreak:
    break_type: BreakType
    match_key: str
    exposure: int
    currency: str
    facts: dict[str, Any] = field(compare=False)
    evidence: Evidence = NO_EVIDENCE

    def __post_init__(self) -> None:
        # Exposure is the size of the money in question, whichever way it moves.
        object.__setattr__(self, "exposure", abs(self.exposure))


def break_id(report_day_: ReportDay, break_type: BreakType, match_key: str) -> str:
    """A Break's identity: the same disagreement found again is the same Break."""
    parts = (report_day_.provider, report_day_.day.isoformat(), break_type, match_key)
    return f"brk_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


def expected_day(provider: str, payment: LedgerPayment) -> date | None:
    """The Banking Day whose report should list the Payment's settlement, if it settled."""
    if payment.settled_at is None:
        return None
    return report_day(provider, payment.rail, payment.settled_at)


def match(view: DayView) -> list[FoundBreak]:
    """Every Break on the view's day, ordered by Break Type, then match key."""
    index = _Index.of(view)
    found = [
        *missing_at_provider(view, index),
        *timing_mismatches(view, index),
        *amount_mismatches(view, index),
        *duplicates(view, index),
        *status_mismatches(view, index),
        *missing_in_ledger(view, index),
    ]
    return sorted(found, key=lambda b: (b.break_type, b.match_key))


# One function per Break Type.


def missing_at_provider(view: DayView, index: "_Index") -> Iterator[FoundBreak]:
    for payment in index.expected_today:
        if not index.agreeing.get(payment.provider_payment_id):
            yield FoundBreak(
                BreakType.MISSING_AT_PROVIDER,
                payment.provider_payment_id,
                payment.amount,
                payment.currency,
                _facts(
                    view,
                    payment,
                    None,
                    f"settled, but on no report up to {view.report_day.next.day}'s",
                ),
                payment.evidence,
            )


def timing_mismatches(view: DayView, index: "_Index") -> Iterator[FoundBreak]:
    for owned in _owned_lines(view, index):
        payment, line = owned.payment, owned.line
        expected = expected_day(view.provider, payment)
        if line.report_date != expected:
            yield FoundBreak(
                BreakType.TIMING_MISMATCH,
                owned.key,
                payment.amount,
                payment.currency,
                _facts(
                    view,
                    payment,
                    line,
                    f"settled for {expected}'s report, but listed on {line.report_date}'s",
                )
                | owned.related,
                payment.evidence | line.evidence,
            )


def amount_mismatches(view: DayView, index: "_Index") -> Iterator[FoundBreak]:
    for owned in _owned_lines(view, index):
        payment, line = owned.payment, owned.line
        if line.amount != payment.signed_amount:
            yield FoundBreak(
                BreakType.AMOUNT_MISMATCH,
                owned.key,
                line.amount - payment.signed_amount,
                payment.currency,
                _facts(
                    view,
                    payment,
                    line,
                    f"reported as {line.amount}, but {payment.signed_amount} settled",
                )
                | owned.related,
                payment.evidence | line.evidence,
            )


def duplicates(view: DayView, index: "_Index") -> Iterator[FoundBreak]:
    for line, payment in index.todays_lines:
        if payment is None or not _agrees(line, payment):
            continue
        first = index.agreeing[payment.provider_payment_id][0]
        if first != line:
            yield FoundBreak(
                BreakType.DUPLICATE,
                line.line_id,
                line.amount,
                line.currency,
                _facts(
                    view,
                    payment,
                    line,
                    f"a second settlement line; the first is {first.line_id} on "
                    f"{first.report_date}'s report",
                ),
                payment.evidence | first.evidence | line.evidence,
            )
    for reissue in sorted(view.reissues, key=lambda r: r.report_id):
        for currency, difference in sorted(_differences(index.ingested_today, reissue).items()):
            yield FoundBreak(
                BreakType.DUPLICATE,
                f"report:{reissue.body_sha256}:{currency}",
                difference.exposure,
                currency,
                {
                    "detail": f"{view.day}'s report was sent again with other lines",
                    "reissue_sha256": reissue.body_sha256,
                    "ingested_lines": [line.stated.as_fact() for line in difference.ingested],
                    "reissued_lines": [line.as_fact() for line in difference.reissued],
                },
                Evidence(
                    reports=(view.report_id, reissue.report_id),
                    report_lines=tuple(line.id for line in difference.ingested),
                ),
            )


def status_mismatches(view: DayView, index: "_Index") -> Iterator[FoundBreak]:
    for line, payment in index.todays_lines:
        if (
            payment is not None
            and _agrees(line, payment)
            and index.agreeing[payment.provider_payment_id][0] == line
            and payment.settled_at is None
        ):
            yield FoundBreak(
                BreakType.STATUS_MISMATCH,
                payment.provider_payment_id,
                line.amount,
                line.currency,
                _facts(view, payment, line, f"reported settled, but {payment.state} in the Ledger"),
                payment.evidence | line.evidence,
            )
    for adjustment in index.adjustments:
        if adjustment.outcome in (None, AdjustmentOutcome.PENDING):
            yield _adjustment_break(
                view,
                index,
                adjustment,
                BreakType.STATUS_MISMATCH,
                "Provider Adjustment still pending: its Payment isn't settled in the Ledger",
            )


def missing_in_ledger(view: DayView, index: "_Index") -> Iterator[FoundBreak]:
    for line, payment in index.todays_lines:
        if payment is None or not _agrees(line, payment):
            yield FoundBreak(
                BreakType.MISSING_IN_LEDGER,
                line.line_id,
                line.amount,
                line.currency,
                _facts(
                    view,
                    payment,
                    line,
                    "no Payment in the Ledger matches this line"
                    if payment is None
                    else "the line contradicts the Payment it names",
                ),
                line.evidence | (NO_EVIDENCE if payment is None else payment.evidence),
            )
    for adjustment in index.adjustments:
        if adjustment.outcome is AdjustmentOutcome.UNMATCHED:
            yield _adjustment_break(
                view,
                index,
                adjustment,
                BreakType.MISSING_IN_LEDGER,
                f"Provider Adjustment left unmatched: {adjustment.detail}",
            )


# What the rules share.


@dataclass(frozen=True)
class _Index:
    """The view, arranged for the rules: each Payment's agreeing settlement lines in order,
    D's settlement lines with the Payment each names, the Payments expected on D."""

    agreeing: Mapping[str, list[StatedLine]]
    todays_lines: tuple[tuple[StatedLine, LedgerPayment | None], ...]
    expected_today: tuple[LedgerPayment, ...]
    adjustments: tuple[AdjustmentLine, ...]
    ingested_today: Mapping[str, StatedLine]

    @classmethod
    def of(cls, view: DayView) -> "_Index":
        lines = sorted(view.settlement_lines, key=lambda line: line.order)
        agreeing: dict[str, list[StatedLine]] = {}
        for line in lines:
            payment = view.payments.get(line.provider_payment_id)
            if payment is not None and _agrees(line, payment):
                agreeing.setdefault(payment.provider_payment_id, []).append(line)
        adjustments = tuple(sorted(view.adjustment_lines, key=lambda a: a.line.order))
        return cls(
            agreeing=agreeing,
            todays_lines=tuple(
                (line, view.payments.get(line.provider_payment_id))
                for line in lines
                if line.report_date == view.day
            ),
            expected_today=tuple(
                payment
                for payment in sorted(view.payments.values(), key=lambda p: p.provider_payment_id)
                if expected_day(view.provider, payment) == view.day
            ),
            adjustments=adjustments,
            ingested_today={
                line.line_id: line
                for line in (*lines, *(a.line for a in adjustments))
                if line.report_date == view.day
            },
        )


@dataclass(frozen=True)
class _Owned:
    """A settled Payment's line, judged on this run under `key`, and the Break on its
    expected day it's related to, if any."""

    payment: LedgerPayment
    line: StatedLine
    key: str
    related: dict[str, Any]


def _owned_lines(view: DayView, index: _Index) -> Iterator[_Owned]:
    """The Payments' lines whose timing and amount this run judges: the Payments expected on
    D with a line up to N's report (keyed by Payment), and D's lines for Payments whose
    expected day is more than one Banking Day earlier (keyed by line; the expected day's run
    found them missing at the Provider)."""
    for expected_payment in index.expected_today:
        lines = index.agreeing.get(expected_payment.provider_payment_id)
        if lines:
            yield _Owned(expected_payment, lines[0], expected_payment.provider_payment_id, {})
    for line, payment in index.todays_lines:
        if payment is None or not _agrees(line, payment):
            continue
        expected = expected_day(view.provider, payment)
        first = index.agreeing[payment.provider_payment_id][0]
        if first == line and expected is not None and view.day > next_banking_day(expected):
            missing = break_id(
                ReportDay(view.provider, expected),
                BreakType.MISSING_AT_PROVIDER,
                payment.provider_payment_id,
            )
            yield _Owned(payment, line, line.line_id, {"related_breaks": [missing]})


def _agrees(line: StatedLine, payment: LedgerPayment) -> bool:
    stated = (line.idempotency_key, line.rail, line.direction, line.currency)
    return stated == (payment.idempotency_key, payment.rail, payment.direction, payment.currency)


def _adjustment_break(
    view: DayView, index: _Index, adjustment: AdjustmentLine, kind: BreakType, detail: str
) -> FoundBreak:
    line = adjustment.line
    named = view.payments.get(line.provider_payment_id)
    payment = named if named is not None and adjustment.payment_id == named.id else None
    evidence = line.evidence | Evidence(
        provider_adjustments=adjustment.decisions,
        payments=() if adjustment.payment_id is None else (adjustment.payment_id,),
    )
    return FoundBreak(
        kind,
        line.line_id,
        line.amount,
        line.currency,
        _facts(view, payment, line, detail)
        | {"adjustment_outcome": adjustment.outcome or AdjustmentOutcome.PENDING},
        evidence | (NO_EVIDENCE if payment is None else payment.evidence),
    )


def _facts(
    view: DayView, payment: LedgerPayment | None, line: StatedLine | None, detail: str
) -> dict[str, Any]:
    known: dict[str, Any] = {"detail": detail}
    if payment is not None:
        expected = expected_day(view.provider, payment)
        known |= {
            "payment_id": payment.id,
            "provider_payment_id": payment.provider_payment_id,
            "idempotency_key": payment.idempotency_key,
            "ledger_state": payment.state,
            "ledger_amount": None if expected is None else payment.signed_amount,
            "ledger_settled_at": None
            if payment.settled_at is None
            else payment.settled_at.isoformat(),
            "expected_report_day": None if expected is None else expected.isoformat(),
        }
    if line is not None:
        known |= {
            "line_id": line.line_id,
            "line_type": line.type,
            "reported_on": line.report_date.isoformat(),
            "reported_amount": line.amount,
            "reported_settled_at": line.settled_at.isoformat(),
        }
    return known


@dataclass
class _Difference:
    exposure: int = 0
    ingested: list[StatedLine] = field(default_factory=list)
    reissued: list[RestatedLine] = field(default_factory=list)


def _differences(ingested: Mapping[str, StatedLine], reissue: Reissue) -> dict[str, _Difference]:
    """Per currency, how much money a re-issue states differently from the ingested report
    (a line on one side only counts in full), and the lines that differ on each side."""
    restated = {line.line_id: line for line in reissue.lines}
    found: dict[str, _Difference] = {}
    for line_id in sorted(ingested.keys() | restated.keys()):
        before, after = ingested.get(line_id), restated.get(line_id)
        if before is not None and after is not None and before.stated == after:
            continue
        if before is not None and after is not None and before.currency == after.currency:
            difference = found.setdefault(before.currency, _Difference())
            difference.exposure += abs(after.amount - before.amount)
        else:  # on one side only, or in another currency: all of it differs
            for currency, amount in (
                *([(before.currency, before.amount)] if before else []),
                *([(after.currency, after.amount)] if after else []),
            ):
                found.setdefault(currency, _Difference()).exposure += abs(amount)
        if before is not None:
            found[before.currency].ingested.append(before)
        if after is not None:
            found[after.currency].reissued.append(after)
    return found
