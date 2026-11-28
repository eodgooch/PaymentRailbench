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
- `provider_adjustment`: `after` Banking Days after the settlement's report day, an
  adjustment line at noon New York claws back `amount`: - for a fee correction, + for a
  recalled part of a Payout. It names the original line and Payment.

Lines are ordered by `settled_at`, then line ID. Each Fault is injected on the one report
where it shows, and named there, for the Fault Manifest.
"""

import csv
import hashlib
import io
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from railbench.common.calendar import add_banking_days, at_new_york
from railbench.common.settlement_reports import COLUMNS, cutoff, report_day
from railbench.simulator.model import (
    FaultPlan,
    MissingReportLine,
    ProviderAdjustment,
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
class _Line:
    line_id: str
    type: str
    payment: Settled
    amount: int
    settled_at: datetime
    original_line_id: str = ""
    reason: str = ""


def settlement_line_id(idempotency_key: str) -> str:
    """The line ID of a Payment's settlement line: the same in every run of a Scenario."""
    digest = hashlib.sha256(f"line\x00{idempotency_key}\x00settlement".encode()).hexdigest()
    return f"simline_{digest[:24]}"


def adjustment_line_id(idempotency_key: str, fault_id: str) -> str:
    digest = hashlib.sha256(f"line\x00{idempotency_key}\x00adjustment\x00{fault_id}".encode())
    return f"simline_{digest.hexdigest()[:24]}"


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _placement(provider: str, payment: Settled) -> tuple[date, date | None, datetime]:
    """The Banking Day the settlement counts toward, the day its line lands on (None when
    it's left off), and the time the line says it settled."""
    proper = report_day(provider, payment.rail, payment.settled_at)
    if any(isinstance(f, MissingReportLine) for f in payment.faults):
        return proper, None, payment.settled_at
    if any(isinstance(f, SettlementAfterCutoff) for f in payment.faults):
        booked = at_new_york(proper, cutoff(provider, payment.rail))
        return proper, report_day(provider, payment.rail, booked), booked
    return proper, proper, payment.settled_at


def _adjustments(provider: str, payment: Settled) -> list[tuple[date, ProviderAdjustment]]:
    proper = report_day(provider, payment.rail, payment.settled_at)
    return [
        (add_banking_days(proper, fault.after), fault)
        for fault in payment.faults
        if isinstance(fault, ProviderAdjustment)
    ]


def content_days(provider: str, payment: Settled) -> set[date]:
    """Every Banking Day whose report this Payment (or a Fault on it) shows on."""
    proper, lands, _ = _placement(provider, payment)
    return {
        proper,
        *([lands] if lands else []),
        *(day for day, _ in _adjustments(provider, payment)),
    }


def build_report(provider: str, day: date, settled: Iterable[Settled]) -> BuiltReport:
    """The Settlement Report `provider` publishes for Banking Day `day`."""
    lines: list[_Line] = []
    injected: list[InjectedReportFault] = []
    for payment in settled:
        proper, lands, booked = _placement(provider, payment)
        line_id = settlement_line_id(payment.idempotency_key)
        found = _payment_lines(provider, day, payment, proper, lands, booked, line_id)
        lines.extend(found[0])
        injected.extend(found[1])
    lines.sort(key=lambda line: (line.settled_at, line.line_id))
    injected.sort(key=lambda found: (found.line_id, found.fault.id))
    return BuiltReport(provider, day, _csv(provider, day, lines), tuple(injected))


def _payment_lines(
    provider: str,
    day: date,
    payment: Settled,
    proper: date,
    lands: date | None,
    booked: datetime,
    line_id: str,
) -> tuple[list[_Line], list[InjectedReportFault]]:
    lines: list[_Line] = []
    injected: list[InjectedReportFault] = []
    where = f"{provider}'s Settlement Report for {day.isoformat()}"
    who = f"{payment.rail.upper()} {payment.direction} {payment.public_id}"

    def inject(
        fault: FaultPlan, event: str, params: dict[str, Any], cause: str, line: str = line_id
    ) -> None:
        base = fault.model_dump(mode="json", exclude={"id", "kind", "event"})
        injected.append(
            InjectedReportFault(payment, fault, line, event, {**base, **params}, fault.kind, cause)
        )

    if lands == day:
        amount = payment.signed_amount
        for fault in payment.faults:
            if isinstance(fault, WrongReportAmount):
                size = payment.amount + fault.by
                amount = size if amount > 0 else -size
                inject(
                    fault,
                    LINE_EVENT,
                    {"report_date": day.isoformat()},
                    f"The Simulator reported {who} on {where} as {size} {payment.currency} "
                    f"Minor Units, {fault.by:+d} off the {payment.amount} that settled "
                    f"(line {line_id}).",
                )
            if isinstance(fault, SettlementAfterCutoff):
                inject(
                    fault,
                    LINE_EVENT,
                    {"report_date": day.isoformat(), "banking_day": proper.isoformat()},
                    f"The Simulator booked {who}, which its Provider Event said settled at "
                    f"{_instant(payment.settled_at)}, at its {payment.rail.upper()} Cutoff "
                    f"({_instant(booked)}), so its line {line_id} landed on {where} instead "
                    f"of {proper.isoformat()}'s (settlement after Cutoff).",
                )
        lines.append(_Line(line_id, "settlement", payment, amount, booked))
    if lands is None and proper == day:
        for fault in payment.faults:
            if isinstance(fault, MissingReportLine):
                inject(
                    fault,
                    LINE_EVENT,
                    {"report_date": day.isoformat()},
                    f"The Simulator left {who}, settled at {_instant(payment.settled_at)}, off "
                    f"{where} (missing report line {line_id}).",
                )
    for adjusted_on, fault in _adjustments(provider, payment):
        if adjusted_on != day:
            continue
        adjustment_id = adjustment_line_id(payment.idempotency_key, fault.id)
        recall = fault.reason == "payout_recall"
        lines.append(
            _Line(
                adjustment_id,
                "adjustment",
                payment,
                fault.amount if recall else -fault.amount,
                at_new_york(day, ADJUSTMENT_TIME),
                original_line_id=line_id,
                reason=fault.reason,
            )
        )
        what = (
            "recalled part of the Payout, crediting it back"
            if recall
            else "corrected its fee, debiting Mintwharf"
        )
        inject(
            fault,
            ADJUSTMENT_EVENT,
            {"report_date": day.isoformat()},
            f"The Simulator {what}: a Provider Adjustment of {fault.amount} "
            f"{payment.currency} Minor Units against {who} (settled {proper.isoformat()}, "
            f"line {line_id}), reported as line {adjustment_id} on {where}.",
            line=adjustment_id,
        )
    return lines, injected


def _csv(provider: str, day: date, lines: list[_Line]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for line in lines:
        payment = line.payment
        writer.writerow(
            (
                day.isoformat(),
                provider,
                line.line_id,
                line.type,
                payment.public_id,
                payment.idempotency_key,
                payment.rail,
                payment.direction,
                payment.currency,
                str(line.amount),
                _instant(line.settled_at),
                line.original_line_id,
                line.reason,
            )
        )
    return out.getvalue().encode()
