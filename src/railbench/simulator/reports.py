"""Building a Provider's Settlement Report for one Banking Day (SPEC §6, ADR 0014).

Pure: what settled (and the report Faults planned on it) in, the CSV's bytes and the report
Faults it injected out, so the same settlements give byte-identical reports. The format and
the Cutoff rule are the shared contract in `railbench.common.settlement_reports`.

- Every settled Payment has one settlement line, on the report of the Banking Day it counts
  toward (`report_day`): + for a pull, - for a push. Declined and canceled Payments never
  settled and aren't listed. A Return isn't listed either: it reaches the Ledger as a
  Provider Event, and the Payment's settlement line stays on the day it settled.
- `missing_report_line`: the line is left off.
- `wrong_report_amount`: the line's amount is off by `by` (in size; its sign is kept).
- `settlement_after_cutoff`: the Provider books the settlement at its Rail's Cutoff on the
  day it should have counted toward, so the line, stamped with that time, lands on the
  next Banking Day's report.
- `duplicate_report_line`: the line is listed twice on the report it lands on, the second
  time under its own line ID (a line ID appears once per report).
- `provider_adjustment`: `after` Banking Days after the settlement's report day, an
  adjustment line at noon New York for `amount`, signed by its reason (- for a fee
  correction, + for a recalled part of a Payout). It names the original line and Payment.
- `orphan_adjustment`: the same, but naming a Payment ID and line the Provider never made
  for Mintwharf, with no Idempotency Key: an adjustment nothing in the Ledger expects.

Lines are ordered by `settled_at`, then line ID. Each Fault is injected on the one report
where it shows, and named there, for the Fault Manifest.
"""

import csv
import hashlib
import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any

from railbench.common.calendar import add_banking_days, at_new_york
from railbench.common.settlement_reports import COLUMNS, AdjustmentReason, cutoff, report_day
from railbench.simulator.model import (
    DuplicateReportLine,
    FaultPlan,
    MissingReportLine,
    OrphanAdjustment,
    PlannedAdjustment,
    SettlementAfterCutoff,
    WrongReportAmount,
)

ADJUSTMENT_TIME = time(12, 0)
LINE_EVENT = "settlement_report.line"
ADJUSTMENT_EVENT = "settlement_report.adjustment"


@dataclass(frozen=True)
class Settled:
    """A Payment as the Simulator settled it, with the Faults planned on it."""

    id: int
    public_id: str
    idempotency_key: str
    rail: str
    direction: str
    currency: str
    amount: int
    settled_at: datetime
    faults: tuple[FaultPlan, ...] = ()

    @property
    def signed_amount(self) -> int:
        return self.amount if self.direction == "pull" else -self.amount


@dataclass(frozen=True)
class InjectedReportFault:
    """A report Fault as it showed on one report, for the Fault Manifest."""

    payment: Settled
    fault: FaultPlan
    line_id: str
    event_type: str
    params: dict[str, Any]
    cause_code: str
    root_cause: str


@dataclass(frozen=True)
class BuiltReport:
    provider: str
    day: date
    body: bytes
    injected: tuple[InjectedReportFault, ...]


@dataclass(frozen=True)
class Placement:
    """Where a Payment's settlement line goes: the Banking Day the settlement counts toward
    (`proper`), the day its line lands on (`lands`, None when it's left off), and the time
    the line says it settled (`booked`)."""

    proper: date
    lands: date | None
    booked: datetime


@dataclass(frozen=True)
class _Line:
    line_id: str
    type: str
    provider_payment_id: str
    idempotency_key: str
    rail: str
    direction: str
    currency: str
    amount: int
    settled_at: datetime
    original_line_id: str = ""
    reason: str = ""


@dataclass
class _Found:
    lines: list[_Line] = field(default_factory=list)
    injected: list[InjectedReportFault] = field(default_factory=list)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:24]


def settlement_line_id(idempotency_key: str) -> str:
    """The line ID of a Payment's settlement line: the same in every run of a Scenario."""
    return f"simline_{_digest('line', idempotency_key, 'settlement')}"


def duplicate_line_id(idempotency_key: str, fault_id: str) -> str:
    """The line ID of a settlement listed a second time."""
    return f"simline_{_digest('line', idempotency_key, 'duplicate', fault_id)}"


def adjustment_line_id(idempotency_key: str, fault_id: str) -> str:
    return f"simline_{_digest('line', idempotency_key, 'adjustment', fault_id)}"


def orphan_payment_id(idempotency_key: str, fault_id: str) -> str:
    """The Payment ID an orphan adjustment names: one the Provider never made."""
    return f"simpay_{_digest('orphan', idempotency_key, fault_id)}"


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def placement(provider: str, payment: Settled) -> Placement:
    proper = report_day(provider, payment.rail, payment.settled_at)
    if any(isinstance(f, MissingReportLine) for f in payment.faults):
        return Placement(proper, None, payment.settled_at)
    if any(isinstance(f, SettlementAfterCutoff) for f in payment.faults):
        booked = at_new_york(proper, cutoff(provider, payment.rail))
        return Placement(proper, report_day(provider, payment.rail, booked), booked)
    return Placement(proper, proper, payment.settled_at)


def _adjustments(
    provider: str, payment: Settled
) -> list[tuple[date, PlannedAdjustment | OrphanAdjustment]]:
    proper = report_day(provider, payment.rail, payment.settled_at)
    return [
        (add_banking_days(proper, fault.after), fault)
        for fault in payment.faults
        if isinstance(fault, PlannedAdjustment | OrphanAdjustment)
    ]


def content_days(provider: str, payment: Settled) -> set[date]:
    """Every Banking Day whose report this Payment (or a Fault on it) shows on."""
    where = placement(provider, payment)
    return {
        where.proper,
        *([where.lands] if where.lands else []),
        *(day for day, _ in _adjustments(provider, payment)),
    }


def build_report(provider: str, day: date, settled: Iterable[Settled]) -> BuiltReport:
    """The Settlement Report `provider` publishes for Banking Day `day`."""
    found = _Found()
    for payment in settled:
        _payment_lines(provider, day, payment, placement(provider, payment), found)
    lines = sorted(found.lines, key=lambda line: (line.settled_at, line.line_id))
    injected = sorted(found.injected, key=lambda f: (f.line_id, f.fault.id))
    return BuiltReport(provider, day, _csv(provider, day, lines), tuple(injected))


def _payment_lines(
    provider: str, day: date, payment: Settled, where: Placement, found: _Found
) -> None:
    line_id = settlement_line_id(payment.idempotency_key)
    report = f"{provider}'s Settlement Report for {day.isoformat()}"
    who = f"{payment.rail.upper()} {payment.direction} {payment.public_id}"
    on_day = {"report_date": day.isoformat()}

    def inject(
        fault: FaultPlan, event: str, params: dict[str, Any], cause: str, line: str = line_id
    ) -> None:
        base = fault.model_dump(mode="json", exclude={"id", "kind", "event"})
        found.injected.append(
            InjectedReportFault(payment, fault, line, event, {**base, **params}, fault.kind, cause)
        )

    def line(line_id: str, type_: str, amount: int, at: datetime, **more: str) -> _Line:
        return _Line(
            line_id=line_id,
            type=type_,
            provider_payment_id=more.pop("provider_payment_id", payment.public_id),
            idempotency_key=more.pop("idempotency_key", payment.idempotency_key),
            rail=payment.rail,
            direction=payment.direction,
            currency=payment.currency,
            amount=amount,
            settled_at=at,
            **more,
        )

    if where.lands == day:
        amount = payment.signed_amount
        for fault in payment.faults:
            if isinstance(fault, WrongReportAmount):
                size = payment.amount + fault.by
                amount = size if amount > 0 else -size
                inject(
                    fault,
                    LINE_EVENT,
                    on_day,
                    f"The Simulator reported {who} on {report} as {size} {payment.currency} "
                    f"Minor Units, {fault.by:+d} off the {payment.amount} that settled "
                    f"(line {line_id}).",
                )
            if isinstance(fault, SettlementAfterCutoff):
                inject(
                    fault,
                    LINE_EVENT,
                    {**on_day, "banking_day": where.proper.isoformat()},
                    f"The Simulator booked {who}, which its Provider Event said settled at "
                    f"{_instant(payment.settled_at)}, at its {payment.rail.upper()} Cutoff "
                    f"({_instant(where.booked)}), so its line {line_id} landed on {report} "
                    f"instead of {where.proper.isoformat()}'s (settlement after Cutoff).",
                )
        found.lines.append(line(line_id, "settlement", amount, where.booked))
        for fault in payment.faults:
            if isinstance(fault, DuplicateReportLine):
                again = duplicate_line_id(payment.idempotency_key, fault.id)
                found.lines.append(line(again, "settlement", amount, where.booked))
                inject(
                    fault,
                    LINE_EVENT,
                    on_day,
                    f"The Simulator listed {who}, settled at {_instant(payment.settled_at)}, "
                    f"twice on {report}: as line {line_id} and again as line {again} "
                    "(duplicate report line).",
                    line=again,
                )
    if where.lands is None and where.proper == day:
        for fault in payment.faults:
            if isinstance(fault, MissingReportLine):
                inject(
                    fault,
                    LINE_EVENT,
                    on_day,
                    f"The Simulator left {who}, settled at {_instant(payment.settled_at)}, "
                    f"off {report} (missing report line {line_id}).",
                )
    for adjusted_on, fault in _adjustments(provider, payment):
        if adjusted_on != day:
            continue
        adjustment_id = adjustment_line_id(payment.idempotency_key, fault.id)
        at = at_new_york(day, ADJUSTMENT_TIME)
        if isinstance(fault, OrphanAdjustment):
            orphan = orphan_payment_id(payment.idempotency_key, fault.id)
            reason = AdjustmentReason.FEE_CORRECTION
            found.lines.append(
                line(
                    adjustment_id,
                    "adjustment",
                    reason.sign * fault.amount,
                    at,
                    provider_payment_id=orphan,
                    idempotency_key="",
                    original_line_id=settlement_line_id(orphan),
                    reason=reason.value,
                )
            )
            inject(
                fault,
                ADJUSTMENT_EVENT,
                on_day,
                f"The Simulator reported a fee correction of {fault.amount} "
                f"{payment.currency} Minor Units against {orphan}, a Payment it never made "
                f"for Mintwharf, as line {adjustment_id} on {report}: an adjustment with no "
                "matching expectation in the Ledger (orphan adjustment).",
                line=adjustment_id,
            )
            continue
        found.lines.append(
            line(
                adjustment_id,
                "adjustment",
                fault.reason.sign * fault.amount,
                at,
                original_line_id=line_id,
                reason=fault.reason.value,
            )
        )
        what = (
            "recalled part of the Payout from its recipient, paying it back into "
            "Mintwharf's account"
            if fault.reason is AdjustmentReason.PAYOUT_RECALL
            else "corrected its fee, debiting Mintwharf's account"
        )
        inject(
            fault,
            ADJUSTMENT_EVENT,
            on_day,
            f"The Simulator {what}: a Provider Adjustment of {fault.amount} "
            f"{payment.currency} Minor Units against {who} (settled "
            f"{where.proper.isoformat()}, line {line_id}), reported as line {adjustment_id} "
            f"on {report}.",
            line=adjustment_id,
        )


def _csv(provider: str, day: date, lines: list[_Line]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for line in lines:
        writer.writerow(
            (
                day.isoformat(),
                provider,
                line.line_id,
                line.type,
                line.provider_payment_id,
                line.idempotency_key,
                line.rail,
                line.direction,
                line.currency,
                str(line.amount),
                _instant(line.settled_at),
                line.original_line_id,
                line.reason,
            )
        )
    return out.getvalue().encode()
