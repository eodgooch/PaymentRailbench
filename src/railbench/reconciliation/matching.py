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
position); any other agreeing line for it is a duplicate. One rule per Break Type:

- missing at Provider: a Payment the Ledger recorded settled, expected on D, with no line on
  any report up to N (one Banking Day's grace, so a late line is a timing mismatch instead);
- timing mismatch: its line is on another report than D's; raised on D's run when the line
  is on a report up to N, or on the line's own report's run when it's later than that;
- amount mismatch: its line's amount isn't what settled (+ a pull, - a push), raised with the
  timing rule's run;
- duplicate: a second settlement line for one Payment (raised on the report it's on), or a
  re-issue of D's report that states other lines than the one ingested (one per currency);
- status mismatch: a settlement line for a Payment the Ledger hadn't recorded settled (still
  in flight, declined or canceled), or a Provider Adjustment still pending, its Payment not
  settled in the Ledger;
- missing in Ledger: a settlement line naming no Payment the Ledger has (or one that
  contradicts the Payment it names), or a Provider Adjustment the Ledger left unmatched.

A booked Provider Adjustment agrees with the Ledger: no Break. Exposure is in Minor Units,
never negative: the size of the money in question (the difference, for an amount mismatch).
"""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from railbench.common.calendar import next_banking_day
from railbench.common.settlement_reports import report_day


class BreakType(StrEnum):
    MISSING_IN_LEDGER = "missing_in_ledger"
    MISSING_AT_PROVIDER = "missing_at_provider"
    AMOUNT_MISMATCH = "amount_mismatch"
    TIMING_MISMATCH = "timing_mismatch"
    DUPLICATE = "duplicate"
    STATUS_MISMATCH = "status_mismatch"


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


@dataclass(frozen=True)
class LedgerPayment:
    """A Payment as the Ledger knew it: its current state and, once settled, when (the
    Effective Time of its `settled` state change)."""

    id: int
    provider_payment_id: str
    idempotency_key: str
    rail: str
    direction: str
    currency: str
    amount: int
    state: str
    settled_at: datetime | None
    evidence: Evidence = Evidence()

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
    type: str
    provider_payment_id: str
    idempotency_key: str
    rail: str
    direction: str
    currency: str
    amount: int
    settled_at: datetime

    @property
    def evidence(self) -> Evidence:
        return Evidence(reports=(self.report_id,), report_lines=(self.id,))

    @property
    def order(self) -> tuple[date, int, int]:
        return (self.report_date, self.position, self.id)


@dataclass(frozen=True)
class AdjustmentLine:
    """A Provider Adjustment line on D's report, with the Ledger's latest decision about it
    (None: no decision recorded yet, which counts as pending)."""

    line: StatedLine
    outcome: str | None
    decisions: tuple[int, ...] = ()
    payment_id: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class RestatedLine:
    line_id: str
    currency: str
    amount: int


@dataclass(frozen=True)
class Reissue:
    """Another version of D's report the Provider sent after one was ingested (kept as a
    `conflict`), with the lines it states."""

    report_id: int
    body_sha256: str
    lines: tuple[RestatedLine, ...]


@dataclass(frozen=True)
class DayView:
    provider: str
    day: date
    report_id: int  # D's ingested report
    payments: Mapping[str, LedgerPayment]  # by the Provider's Payment ID
    settlement_lines: tuple[StatedLine, ...]  # every one bearing on D, up to N's report
    adjustment_lines: tuple[AdjustmentLine, ...] = ()  # on D's report
    reissues: tuple[Reissue, ...] = ()

    @property
    def next_day(self) -> date:
        return next_banking_day(self.day)


@dataclass(frozen=True)
class FoundBreak:
    break_type: BreakType
    match_key: str
    exposure: int
    currency: str
    facts: dict[str, Any] = field(compare=False)
    evidence: Evidence = Evidence()


def break_id(provider: str, day: date, break_type: BreakType, match_key: str) -> str:
    """A Break's identity: the same disagreement found again is the same Break."""
    digest = hashlib.sha256(
        "\x00".join((provider, day.isoformat(), break_type, match_key)).encode()
    ).hexdigest()
    return f"brk_{digest[:24]}"


def expected_day(provider: str, payment: LedgerPayment) -> date | None:
    """The Banking Day whose report should list the Payment's settlement, if it settled."""
    if payment.settled_at is None:
        return None
    return report_day(provider, payment.rail, payment.settled_at)


def _agrees(line: StatedLine, payment: LedgerPayment) -> bool:
    stated = (line.idempotency_key, line.rail, line.direction, line.currency)
    return stated == (payment.idempotency_key, payment.rail, payment.direction, payment.currency)


def match(view: DayView) -> list[FoundBreak]:
    """Every Break on `view.day`, ordered by Break Type, then match key."""
    found: list[FoundBreak] = []
    provider, day, next_day = view.provider, view.day, view.next_day
    agreeing: dict[str, list[StatedLine]] = {}
    for line in sorted(view.settlement_lines, key=lambda line: line.order):
        payment = view.payments.get(line.provider_payment_id)
        if payment is not None and _agrees(line, payment):
            agreeing.setdefault(payment.provider_payment_id, []).append(line)

    def facts(
        payment: LedgerPayment | None, line: StatedLine | None, detail: str
    ) -> dict[str, Any]:
        known: dict[str, Any] = {"detail": detail}
        if payment is not None:
            expected = expected_day(provider, payment)
            known |= {
                "payment_id": payment.id,
                "provider_payment_id": payment.provider_payment_id,
                "idempotency_key": payment.idempotency_key,
                "ledger_state": payment.state,
                "ledger_amount": payment.signed_amount if expected else None,
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

    def add(
        kind: BreakType,
        key: str,
        exposure: int,
        currency: str,
        known: dict[str, Any],
        evidence: Evidence,
    ) -> None:
        found.append(FoundBreak(kind, key, abs(exposure), currency, known, evidence))

    def settled_line_breaks(payment: LedgerPayment, line: StatedLine, key: str) -> None:
        """The timing and amount rules for a settled Payment's own line."""
        evidence = payment.evidence | line.evidence
        expected = expected_day(provider, payment)
        if line.report_date != expected:
            add(
                BreakType.TIMING_MISMATCH,
                key,
                payment.amount,
                payment.currency,
                facts(
                    payment,
                    line,
                    f"settled for {expected}'s report, but listed on {line.report_date}'s",
                ),
                evidence,
            )
        if line.amount != payment.signed_amount:
            add(
                BreakType.AMOUNT_MISMATCH,
                key,
                line.amount - payment.signed_amount,
                payment.currency,
                facts(
                    payment,
                    line,
                    f"reported as {line.amount}, but {payment.signed_amount} settled",
                ),
                evidence,
            )

    # The Payments the Ledger expects on D's report.
    for payment in sorted(view.payments.values(), key=lambda p: p.provider_payment_id):
        if expected_day(provider, payment) != day:
            continue
        lines = agreeing.get(payment.provider_payment_id, [])
        if not lines:
            add(
                BreakType.MISSING_AT_PROVIDER,
                payment.provider_payment_id,
                payment.amount,
                payment.currency,
                facts(payment, None, f"settled, but on no report up to {next_day}'s"),
                payment.evidence,
            )
        else:
            settled_line_breaks(payment, lines[0], payment.provider_payment_id)

    # The settlement lines on D's report.
    for line in sorted(view.settlement_lines, key=lambda line: line.order):
        if line.report_date != day:
            continue
        payment = view.payments.get(line.provider_payment_id)
        if payment is None or not _agrees(line, payment):
            add(
                BreakType.MISSING_IN_LEDGER,
                line.line_id,
                line.amount,
                line.currency,
                facts(
                    payment,
                    line,
                    "no Payment in the Ledger matches this line"
                    if payment is None
                    else "the line contradicts the Payment it names",
                ),
                line.evidence | (Evidence() if payment is None else payment.evidence),
            )
            continue
        first = agreeing[payment.provider_payment_id][0]
        if first != line:
            add(
                BreakType.DUPLICATE,
                line.line_id,
                line.amount,
                line.currency,
                facts(
                    payment,
                    line,
                    f"a second settlement line; the first is {first.line_id} on "
                    f"{first.report_date}'s report",
                ),
                payment.evidence | first.evidence | line.evidence,
            )
            continue
        expected = expected_day(provider, payment)
        if expected is None:
            add(
                BreakType.STATUS_MISMATCH,
                payment.provider_payment_id,
                line.amount,
                line.currency,
                facts(
                    payment,
                    line,
                    f"reported settled, but {payment.state} in the Ledger",
                ),
                payment.evidence | line.evidence,
            )
        elif day > next_banking_day(expected):
            # Too late for its expected day's run, which found it missing at the Provider.
            settled_line_breaks(payment, line, line.line_id)

    # The Provider Adjustment lines on D's report.
    for adjustment in sorted(view.adjustment_lines, key=lambda a: a.line.order):
        line = adjustment.line
        evidence = line.evidence | Evidence(
            provider_adjustments=adjustment.decisions,
            payments=() if adjustment.payment_id is None else (adjustment.payment_id,),
        )
        payment = view.payments.get(line.provider_payment_id)
        if payment is not None and adjustment.payment_id == payment.id:
            evidence |= payment.evidence
        else:
            payment = None
        if adjustment.outcome == "booked":
            continue
        unmatched = adjustment.outcome == "unmatched"
        add(
            BreakType.MISSING_IN_LEDGER if unmatched else BreakType.STATUS_MISMATCH,
            line.line_id,
            line.amount,
            line.currency,
            facts(
                payment,
                line,
                f"Provider Adjustment left unmatched: {adjustment.detail}"
                if unmatched
                else "Provider Adjustment still pending: its Payment isn't settled in the Ledger",
            )
            | {"adjustment_outcome": adjustment.outcome or "pending"},
            evidence,
        )

    # Re-issues of D's report.
    ingested = {
        line.line_id: line
        for line in (*view.settlement_lines, *(a.line for a in view.adjustment_lines))
        if line.report_date == day
    }
    for reissue in sorted(view.reissues, key=lambda r: r.report_id):
        for currency, (exposure, line_ids) in sorted(_differences(ingested, reissue).items()):
            add(
                BreakType.DUPLICATE,
                f"report:{reissue.body_sha256}:{currency}",
                exposure,
                currency,
                {
                    "detail": f"{day}'s report was sent again with other lines",
                    "reissue_sha256": reissue.body_sha256,
                },
                Evidence(reports=(view.report_id, reissue.report_id), report_lines=line_ids),
            )

    return sorted(found, key=lambda b: (b.break_type, b.match_key))


def _differences(
    ingested: Mapping[str, StatedLine], reissue: Reissue
) -> dict[str, tuple[int, tuple[int, ...]]]:
    """Per currency, how much money a re-issue states differently from the ingested report
    (a line on one side only counts in full), and the ingested lines that differ."""
    restated = {line.line_id: line for line in reissue.lines}
    exposure: dict[str, int] = {}
    lines: dict[str, list[int]] = {}
    for line_id in sorted(ingested.keys() | restated.keys()):
        before, after = ingested.get(line_id), restated.get(line_id)
        sides = [(s.currency, s.amount) for s in (before, after) if s is not None]
        if len(sides) == 2 and sides[0] == sides[1]:
            continue
        if len(sides) == 2 and sides[0][0] == sides[1][0]:
            sides = [(sides[0][0], sides[1][1] - sides[0][1])]
        for currency, amount in sides:
            exposure[currency] = exposure.get(currency, 0) + abs(amount)
            lines.setdefault(currency, [])
        if before is not None:
            lines[before.currency].append(before.id)
    return {currency: (exposure[currency], _ids(lines[currency])) for currency in exposure}
