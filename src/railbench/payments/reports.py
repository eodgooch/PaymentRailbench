"""Ingesting Settlement Reports and booking Provider Adjustments (SPEC §4.5, §6, ADR 0014).

The `settlement-report-ingest` job runs on the Ledger's Clock. Each run first retries the
Provider Adjustments still pending (step 4), then, for every Banking Day from the first one
this Provider was asked to make a Payment on, once that day's report is due
(`report_published_at`), until one is ingested, fetches the report through the Provider
interface, verifies its signature (like a webhook; a bad one is logged and stored nowhere)
and receives it. Receiving one report is one database transaction, under a lock on
(Provider, report date):

1. The raw body and headers are stored, unique by (Provider, report date, SHA-256): the
   same bytes again are a duplicate and do nothing.
2. A body the adapter can't read is kept as `malformed`, with why. A readable one for a day
   already ingested, but with other bytes (a Provider re-issuing it), is kept as a
   `conflict`: nothing is re-ingested or corrected; Reconciliation (#9) works it.
3. Otherwise it's `ingested`: every line is stored as the Provider stated it. Settlement
   lines change no balance, whatever they say: a missing, late or wrong line is evidence
   for Reconciliation, never a correction the Ledger makes on its own.
4. Each Provider Adjustment line is decided against the original Payment, found by the
   Provider's Payment ID, and the decision appended (`provider_adjustments`):
   - `unmatched`, never booked, if there's no such Payment, the line contradicts it
     (Idempotency Key, Rail, direction, currency, or a reason the money can't move that way
     for it), the adjustments against it would exceed what settled, or it ended (declined
     or canceled) without settling;
   - `pending` if it agrees but the Ledger hasn't recorded it settled yet (its `settled`
     Provider Event late or out of order). Each later run decides it again, under the
     Payment's lock, until it's booked or unmatched; while the Payment neither settles nor
     ends, it stays pending, which Reconciliation sees;
   - otherwise `booked`: two posted Transactions at the line's `settled_at`, with this job
     as Actor, through the Payment's Clearing Account, so it nets to zero at once:
     - fee correction (money out of Mintwharf's account; Mintwharf bears it): Provider Fees
       debit, Clearing Account credit; then Clearing Account debit, Bank Cash credit;
     - Payout recall (part of a Payout pulled back from its recipient into Mintwharf's
       account; Mintwharf owes the seller that part again): Clearing Account debit,
       Customer Balance credit; then Bank Cash debit, Clearing Account credit.
   Neither takes money from a Customer Balance, so neither can leave one short.
"""

import hashlib
import logging
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, fields
from datetime import date
from itertools import takewhile
from typing import Any

from sqlalchemy import Connection, Engine, Row, Select, func, insert, select

from railbench.common.calendar import banking_days_from, new_york_date
from railbench.common.clock import Clock
from railbench.common.locks import LockSpace, lock, lock_name
from railbench.common.settlement_reports import AdjustmentReason, report_published_at
from railbench.common.signing import BadSignature
from railbench.ledger.journal import AccountNotFound, DuplicateAccount, Journal, LedgerError
from railbench.ledger.model import AccountType, NewAccount, NewEntry, NewTransaction, SystemJob
from railbench.payments.accounts import bank_cash_account, clearing_account, ledger_key
from railbench.payments.model import (
    AdjustmentOutcome,
    ProviderAdjustment,
    ReportLineView,
    ReportOutcome,
    ReportReceipt,
    SettlementReportView,
)
from railbench.payments.provider import (
    MalformedReport,
    Provider,
    ProviderError,
    ReportLine,
    SettlementReport,
)
from railbench.payments.state_machine import ENDS_BEFORE_SETTLEMENT, PaymentState
from railbench.payments.tables import (
    payments,
    provider_adjustments,
    settlement_report_lines,
    settlement_reports,
    transitions,
)

log = logging.getLogger(__name__)

INGEST_JOB = SystemJob(name="settlement-report-ingest", version="1.0.0")
# A report line's fields as the Provider stated them: the columns of
# `settlement_report_lines` besides its keys and position, read and written by name.
LINE_FIELDS = tuple(f.name for f in fields(ReportLine))


def provider_fees_account(currency: str) -> str:
    """The expense Account for what Providers charge Mintwharf (extends SPEC §3.1)."""
    return f"provider-fees:{currency.lower()}"


@dataclass(frozen=True)
class _Decision:
    outcome: AdjustmentOutcome
    detail: str = ""
    transaction_id: int | None = None


class SettlementReports:
    def __init__(self, engine: Engine, clock: Clock, journal: Journal, provider: Provider) -> None:
        self._engine = engine
        self._clock = clock
        self._journal = journal
        self._provider = provider

    def ingest_due(self) -> int:
        """Retry the pending Provider Adjustments, then fetch and receive every report
        that's due and not ingested yet. Returns how many reports it received (duplicates
        included). A report not published, unreachable or badly signed is tried again on
        the next run."""
        self.retry_pending()
        now = self._clock.now()
        provider = self._provider
        with self._engine.connect() as connection:
            first = connection.scalar(
                select(func.min(payments.c.recorded_at)).where(payments.c.provider == provider.name)
            )
            ingested = set(
                connection.scalars(
                    select(settlement_reports.c.report_date).where(
                        settlement_reports.c.provider == provider.name,
                        settlement_reports.c.outcome == ReportOutcome.INGESTED,
                    )
                )
            )
        if first is None:
            return 0
        received = 0
        days = banking_days_from(new_york_date(first))
        for day in takewhile(lambda d: report_published_at(d) <= now, days):
            if day in ingested:
                continue
            try:
                fetched = provider.fetch_settlement_report(day)
                if fetched is None:
                    continue
                provider.verify_report(fetched.body, fetched.headers, self._clock.now())
            except (ProviderError, BadSignature) as error:
                log.warning(
                    "Settlement Report not fetched",
                    extra={"provider": provider.name, "day": day.isoformat(), "reason": str(error)},
                )
                continue
            self._receive(day, fetched.body, fetched.headers)
            received += 1
        return received

    def retry_pending(self) -> int:
        """Decide again every Provider Adjustment still pending, each in its own database
        transaction under its Payment's lock. Returns how many were decided."""
        decided = provider_adjustments.alias("decided")
        with self._engine.connect() as connection:
            waiting = connection.execute(
                select(provider_adjustments.c.line_id, provider_adjustments.c.payment_id)
                .where(
                    provider_adjustments.c.outcome == AdjustmentOutcome.PENDING,
                    ~select(decided.c.id)
                    .where(
                        decided.c.line_id == provider_adjustments.c.line_id,
                        decided.c.outcome != AdjustmentOutcome.PENDING,
                    )
                    .exists(),
                )
                .order_by(provider_adjustments.c.id)
            ).all()
        count = 0
        for line_pk, payment_id in waiting:
            with self._engine.begin() as connection:
                lock(connection, LockSpace.PAYMENT, payment_id)
                if _latest(connection, line_pk) is not AdjustmentOutcome.PENDING:
                    continue  # decided meanwhile
                payment = connection.execute(
                    select(payments).where(payments.c.id == payment_id)
                ).one()
                line = _line_from(
                    connection.execute(
                        select(settlement_report_lines).where(
                            settlement_report_lines.c.id == line_pk
                        )
                    ).one()
                )
                decision = self._decide(connection, payment, line)
                if decision.outcome is not AdjustmentOutcome.PENDING:
                    self._record(connection, line_pk, payment_id, line, decision)
                    count += 1
        return count

    def replay(
        self, provider_name: str, day: date, body: bytes, headers: Mapping[str, str]
    ) -> ReportReceipt:
        """Receive a report body that was verified when it was first fetched, without
        verifying it again. In-process only (replaying the Ledger, ADR 0011)."""
        if provider_name != self._provider.name:
            raise ValueError(f"no Provider {provider_name}")
        return self._receive(day, body, headers)

    def reports(self, provider_name: str, day: date) -> list[SettlementReportView]:
        """Every report received for the Provider and day, in the order received."""
        with self._engine.connect() as connection:
            ids = connection.scalars(
                select(settlement_reports.c.id)
                .where(
                    settlement_reports.c.provider == provider_name,
                    settlement_reports.c.report_date == day,
                )
                .order_by(settlement_reports.c.id)
            ).all()
            return [_view(connection, report_id) for report_id in ids]

    def _receive(self, day: date, body: bytes, headers: Mapping[str, str]) -> ReportReceipt:
        provider = self._provider
        sha = hashlib.sha256(body).hexdigest()
        with self._engine.begin() as connection:
            lock_name(connection, LockSpace.REPORT, f"{provider.name}:{day.isoformat()}")
            same = connection.scalar(
                select(settlement_reports.c.id).where(
                    settlement_reports.c.provider == provider.name,
                    settlement_reports.c.report_date == day,
                    settlement_reports.c.body_sha256 == sha,
                )
            )
            if same is not None:
                return ReportReceipt(report=_view(connection, same), duplicate=True)
            parsed: SettlementReport | None = None
            outcome, detail = ReportOutcome.INGESTED, ""
            try:
                parsed = provider.read_settlement_report(day, body)
            except MalformedReport as error:
                outcome, detail = ReportOutcome.MALFORMED, str(error)
            earlier = connection.execute(
                select(settlement_reports.c.id, settlement_reports.c.body_sha256).where(
                    settlement_reports.c.provider == provider.name,
                    settlement_reports.c.report_date == day,
                    settlement_reports.c.outcome == ReportOutcome.INGESTED,
                )
            ).one_or_none()
            if parsed is not None and earlier is not None:
                outcome = ReportOutcome.CONFLICT
                detail = (
                    f"differs from the report ingested for {day.isoformat()} "
                    f"(SHA-256 {earlier.body_sha256})"
                )
            report_id: int = connection.execute(
                insert(settlement_reports)
                .values(
                    provider=provider.name,
                    report_date=day,
                    body=body,
                    body_sha256=sha,
                    headers=dict(headers),
                    received_at=self._clock.now(),
                    outcome=outcome,
                    detail=detail,
                )
                .returning(settlement_reports.c.id)
            ).scalar_one()
            if outcome is not ReportOutcome.INGESTED:
                log.warning(
                    "Settlement Report kept, not ingested",
                    extra={"provider": provider.name, "day": day.isoformat(), "reason": detail},
                )
            elif parsed is not None:
                for position, line in enumerate(parsed.lines, start=1):
                    line_pk = connection.execute(
                        insert(settlement_report_lines)
                        .values(report_id=report_id, position=position, **_line_columns(line))
                        .returning(settlement_report_lines.c.id)
                    ).scalar_one()
                    if line.type == "adjustment":
                        self._adjust(connection, line_pk, line)
            return ReportReceipt(report=_view(connection, report_id), duplicate=False)

    def _adjust(self, connection: Connection, line_pk: int, line: ReportLine) -> None:
        """Decide a newly received Provider Adjustment line, and record the decision."""
        provider = self._provider.name
        payment = connection.execute(
            select(payments).where(
                payments.c.provider == provider,
                payments.c.provider_payment_id == line.provider_payment_id,
            )
        ).one_or_none()
        if payment is None:
            decision = _Decision(
                AdjustmentOutcome.UNMATCHED,
                f"no Payment {line.provider_payment_id} at {provider} in the Ledger",
            )
            self._record(connection, line_pk, None, line, decision)
            return
        lock(connection, LockSpace.PAYMENT, payment.id)
        self._record(connection, line_pk, payment.id, line, self._decide(connection, payment, line))

    def _decide(self, connection: Connection, payment: Row[Any], line: ReportLine) -> _Decision:
        refused = _contradiction(connection, payment, line)
        if refused:
            return _Decision(AdjustmentOutcome.UNMATCHED, refused)
        states = set(
            connection.scalars(
                select(transitions.c.state).where(transitions.c.payment_id == payment.id)
            )
        )
        if PaymentState.SETTLED not in states:
            ended = [state for state in ENDS_BEFORE_SETTLEMENT if state in states]
            if ended:
                return _Decision(
                    AdjustmentOutcome.UNMATCHED,
                    f"Payment {payment.id} was {ended[0]} without settling",
                )
            return _Decision(
                AdjustmentOutcome.PENDING,
                f"waiting for the Ledger to record Payment {payment.id} settled",
            )
        try:
            with connection.begin_nested():
                return _Decision(
                    AdjustmentOutcome.BOOKED,
                    transaction_id=self._book(connection, payment, line),
                )
        except LedgerError as error:
            return _Decision(AdjustmentOutcome.UNMATCHED, f"the Ledger refused it: {error}")

    def _record(
        self,
        connection: Connection,
        line_pk: int,
        payment_id: int | None,
        line: ReportLine,
        decision: _Decision,
    ) -> None:
        if decision.outcome is not AdjustmentOutcome.BOOKED:
            log.warning(
                "Provider Adjustment not booked",
                extra={
                    "line_id": line.line_id,
                    "outcome": decision.outcome,
                    "reason": decision.detail,
                },
            )
        connection.execute(
            insert(provider_adjustments).values(
                line_id=line_pk,
                outcome=decision.outcome,
                payment_id=payment_id,
                transaction_id=decision.transaction_id,
                detail=decision.detail,
                recorded_at=self._clock.now(),
            )
        )

    def _book(self, connection: Connection, payment: Row[Any], line: ReportLine) -> int:
        journal = self._journal.within(connection)
        size = abs(line.amount)
        clearing = clearing_account(payment.provider, payment.rail, payment.currency)
        bank_cash = bank_cash_account(payment.currency)
        if line.reason is AdjustmentReason.FEE_CORRECTION:
            fees = provider_fees_account(payment.currency)
            _ensure_account(journal, fees, AccountType.EXPENSE, payment.currency)
            booked, swept = (fees, clearing), (clearing, bank_cash)
            what = "fee correction"
        else:
            booked, swept = (clearing, payment.customer_account), (bank_cash, clearing)
            what = "Payout recall"
        step = f"adjustment:{line.line_id}"
        kind = f"{payment.rail.upper()} {payment.direction} {payment.provider_payment_id}"
        ids = []
        for key, (debit, credit), description in (
            (step, booked, f"{kind} Provider Adjustment ({what}) {line.line_id}"),
            (f"{step}:bank-cash", swept, f"{kind} Provider Adjustment {line.line_id}, Bank Cash"),
        ):
            ids.append(
                journal.post(
                    NewTransaction(
                        idempotency_key=ledger_key(payment.id, key),
                        actor=INGEST_JOB,
                        effective_at=line.settled_at,
                        description=description,
                        entries=[
                            NewEntry(account=debit, currency=payment.currency, amount=size),
                            NewEntry(account=credit, currency=payment.currency, amount=-size),
                        ],
                    )
                ).transaction.id
            )
        return ids[0]


def _contradiction(connection: Connection, payment: Row[Any], line: ReportLine) -> str:
    """Why this adjustment line can never be booked against `payment`, or ""."""
    stated = (line.idempotency_key, line.rail, line.direction, line.currency)
    recorded = (payment.idempotency_key, payment.rail, payment.direction, payment.currency)
    if stated != recorded:
        return f"the line describes {stated}, but Payment {payment.id} is {recorded}"
    reason = line.reason
    assert reason is not None  # the adapter refuses an adjustment line without one
    if (line.amount > 0) != (reason.sign > 0):
        return f"a {reason} moves money {'in' if reason.sign > 0 else 'out'}, not the other way"
    if reason is AdjustmentReason.PAYOUT_RECALL and payment.direction != "push":
        return "a Payout recall brings part of a push back in, and this is a pull"
    booked = connection.scalars(
        select(settlement_report_lines.c.amount)
        .join(provider_adjustments, provider_adjustments.c.line_id == settlement_report_lines.c.id)
        .where(
            provider_adjustments.c.payment_id == payment.id,
            provider_adjustments.c.outcome == AdjustmentOutcome.BOOKED,
        )
    )
    total = sum(abs(amount) for amount in booked) + abs(line.amount)
    if total > payment.amount:
        return f"adjustments of {total} would claw back more than the {payment.amount} settled"
    return ""


def _latest(connection: Connection, line_pk: int) -> AdjustmentOutcome | None:
    outcome = connection.scalar(
        select(provider_adjustments.c.outcome)
        .where(provider_adjustments.c.line_id == line_pk)
        .order_by(provider_adjustments.c.id.desc())
        .limit(1)
    )
    return None if outcome is None else AdjustmentOutcome(outcome)


def _ensure_account(journal: Journal, code: str, type_: AccountType, currency: str) -> None:
    try:
        journal.account(code)
    except AccountNotFound:
        with suppress(DuplicateAccount):  # opened concurrently; the Journal uses a savepoint
            journal.open_account(NewAccount(code=code, type=type_, currency=currency))


def _line_columns(line: ReportLine) -> dict[str, Any]:
    return {name: getattr(line, name) for name in LINE_FIELDS}


def _line_from(row: Row[Any]) -> ReportLine:
    found = {name: getattr(row, name) for name in LINE_FIELDS}
    if found["reason"] is not None:
        found["reason"] = AdjustmentReason(found["reason"])
    return ReportLine(**found)


def adjustments_query() -> Select[Any]:
    """Every Provider Adjustment decision, oldest first, with the line and report it's
    about."""
    return (
        select(
            provider_adjustments,
            settlement_report_lines.c.line_id.label("report_line_id"),
            settlement_report_lines.c.reason,
            settlement_report_lines.c.amount,
            settlement_report_lines.c.settled_at,
            settlement_reports.c.report_date,
        )
        .join(
            settlement_report_lines,
            settlement_report_lines.c.id == provider_adjustments.c.line_id,
        )
        .join(settlement_reports, settlement_reports.c.id == settlement_report_lines.c.report_id)
        .order_by(provider_adjustments.c.id)
    )


def _adjustment_view(row: Row[Any]) -> ProviderAdjustment:
    return ProviderAdjustment(
        report_date=row.report_date,
        line_id=row.report_line_id,
        reason=AdjustmentReason(row.reason),
        amount=row.amount,
        effective_at=row.settled_at,
        outcome=AdjustmentOutcome(row.outcome),
        payment_id=row.payment_id,
        transaction_id=row.transaction_id,
        detail=row.detail,
        recorded_at=row.recorded_at,
    )


def latest_adjustments(connection: Connection, where: Any) -> dict[int, ProviderAdjustment]:
    """The last decision about each adjustment line matching `where`, by the line's row id."""
    return {
        row.line_id: _adjustment_view(row)
        for row in connection.execute(adjustments_query().where(where))
    }


def payment_adjustments(connection: Connection, payment_id: int) -> list[ProviderAdjustment]:
    """The Provider Adjustments reported against a Payment, each as last decided."""
    return list(
        latest_adjustments(connection, provider_adjustments.c.payment_id == payment_id).values()
    )


def _view(connection: Connection, report_id: int) -> SettlementReportView:
    report = connection.execute(
        select(settlement_reports).where(settlement_reports.c.id == report_id)
    ).one()
    lines = connection.execute(
        select(settlement_report_lines)
        .where(settlement_report_lines.c.report_id == report_id)
        .order_by(settlement_report_lines.c.position)
    ).all()
    decided = latest_adjustments(connection, settlement_report_lines.c.report_id == report_id)
    return SettlementReportView(
        id=report.id,
        provider=report.provider,
        report_date=report.report_date,
        body_sha256=report.body_sha256,
        received_at=report.received_at,
        outcome=ReportOutcome(report.outcome),
        detail=report.detail,
        lines=[
            ReportLineView(
                position=line.position,
                **{name: getattr(line, name) for name in LINE_FIELDS},
                adjustment=decided.get(line.id),
            )
            for line in lines
        ],
    )
