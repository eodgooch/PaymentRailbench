"""Ingesting Settlement Reports and booking Provider Adjustments (SPEC §4.5, §6, ADR 0014).

The `settlement-report-ingest` job runs on the Ledger's Clock. For every Banking Day from the
first one this Provider was asked to make a Payment on, once that day's report is due
(`report_published_at`), until one is ingested, it fetches the report through the Provider
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
4. Each Provider Adjustment line is booked against the original Payment, found by the
   Provider's Payment ID, if the line agrees with it (Idempotency Key, Rail, direction,
   currency), the Ledger has recorded it settled, the reason fits (a fee correction takes
   money out; a Payout recall brings part of a push back in) and the adjustments booked
   against it stay within what settled. Two posted Transactions, both at the line's
   `settled_at`, with this job as Actor, through the Payment's Clearing Account, so it nets
   to zero at once:
   - fee correction (Mintwharf bears it): Provider Fees debit, Clearing Account credit;
     then Clearing Account debit, Bank Cash credit;
   - Payout recall (the seller wasn't paid that part, so Mintwharf owes it again): Clearing
     Account debit, Customer Balance credit; then Bank Cash debit, Clearing Account credit.
   Neither takes money from a Customer Balance, so neither can leave one short. Any other
   adjustment line is stored `unmatched`, with why, and books nothing.
"""

import hashlib
import logging
from collections.abc import Iterator, Mapping
from contextlib import suppress
from datetime import date
from itertools import takewhile
from typing import Any

from sqlalchemy import Connection, Engine, Row, func, insert, select

from railbench.common.calendar import is_banking_day, new_york_date, next_banking_day
from railbench.common.clock import Clock
from railbench.common.locks import LockSpace, lock, lock_name
from railbench.common.settlement_reports import report_published_at
from railbench.common.signing import BadSignature
from railbench.ledger.journal import AccountNotFound, DuplicateAccount, Journal, LedgerError
from railbench.ledger.model import AccountType, NewAccount, NewEntry, NewTransaction, SystemJob
from railbench.payments.model import (
    AdjustmentOutcome,
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
from railbench.payments.service import (
    UnknownProvider,
    adjustment_view,
    adjustments_query,
    bank_cash_account,
    clearing_account,
    ledger_key,
)
from railbench.payments.state_machine import PaymentState
from railbench.payments.tables import (
    payments,
    provider_adjustments,
    settlement_report_lines,
    settlement_reports,
    transitions,
)

log = logging.getLogger(__name__)

INGEST_JOB = SystemJob(name="settlement-report-ingest", version="1.0.0")


def provider_fees_account(currency: str) -> str:
    """The expense Account for what Providers charge Mintwharf (extends SPEC §3.1)."""
    return f"provider-fees:{currency.lower()}"


class SettlementReports:
    def __init__(self, engine: Engine, clock: Clock, journal: Journal, provider: Provider) -> None:
        self._engine = engine
        self._clock = clock
        self._journal = journal
        self._provider = provider

    def ingest_due(self) -> int:
        """Fetch and receive every report that's due and not ingested yet. Returns how many
        it received (duplicates included). A report not published, unreachable or badly
        signed is tried again on the next run."""
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
        due = takewhile(lambda d: report_published_at(d) <= now, _banking_days(first))
        for day in due:
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

    def replay(
        self, provider_name: str, day: date, body: bytes, headers: Mapping[str, str]
    ) -> ReportReceipt:
        """Receive a report body that was verified when it was first fetched, without
        verifying it again. In-process only (replaying the Ledger, ADR 0011)."""
        if provider_name != self._provider.name:
            raise UnknownProvider(f"no Provider {provider_name}")
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
        """Book one Provider Adjustment line against its original Payment, or record why
        it isn't booked."""
        provider = self._provider.name
        payment = connection.execute(
            select(payments).where(
                payments.c.provider == provider,
                payments.c.provider_payment_id == line.provider_payment_id,
            )
        ).one_or_none()
        transaction_id: int | None = None
        if payment is None:
            detail = f"no Payment {line.provider_payment_id} at {provider} in the Ledger"
        else:
            lock(connection, LockSpace.PAYMENT, payment.id)
            detail = _refusal(connection, payment, line)
            if not detail:
                try:
                    with connection.begin_nested():
                        transaction_id = self._book(connection, payment, line)
                except LedgerError as error:
                    detail = f"the Ledger refused it: {error}"
        if detail:
            log.warning(
                "Provider Adjustment not booked",
                extra={"line_id": line.line_id, "reason": detail},
            )
        connection.execute(
            insert(provider_adjustments).values(
                line_id=line_pk,
                outcome=AdjustmentOutcome.UNMATCHED if detail else AdjustmentOutcome.BOOKED,
                payment_id=None if payment is None else payment.id,
                transaction_id=transaction_id,
                detail=detail,
                recorded_at=self._clock.now(),
            )
        )

    def _book(self, connection: Connection, payment: Row[Any], line: ReportLine) -> int:
        journal = self._journal.within(connection)
        size = abs(line.amount)
        clearing = clearing_account(payment.provider, payment.rail, payment.currency)
        bank_cash = bank_cash_account(payment.currency)
        if line.reason == "fee_correction":
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


def _refusal(connection: Connection, payment: Row[Any], line: ReportLine) -> str:
    """Why this adjustment line can't be booked against `payment`, or "" if it can."""
    stated = (line.idempotency_key, line.rail, line.direction, line.currency)
    recorded = (payment.idempotency_key, payment.rail, payment.direction, payment.currency)
    if stated != recorded:
        return f"the line describes {stated}, but Payment {payment.id} is {recorded}"
    settled = connection.scalar(
        select(transitions.c.id).where(
            transitions.c.payment_id == payment.id, transitions.c.state == PaymentState.SETTLED
        )
    )
    if settled is None:
        return f"the Ledger hasn't recorded Payment {payment.id} settled"
    if line.reason == "fee_correction" and line.amount > 0:
        return "a fee correction takes money out of Mintwharf's account, not in"
    if line.reason == "payout_recall" and (payment.direction != "push" or line.amount < 0):
        return "a Payout recall brings part of a push back in"
    booked = connection.execute(
        select(settlement_report_lines.c.amount)
        .join(provider_adjustments, provider_adjustments.c.line_id == settlement_report_lines.c.id)
        .where(
            provider_adjustments.c.payment_id == payment.id,
            provider_adjustments.c.outcome == AdjustmentOutcome.BOOKED,
        )
    ).scalars()
    total = sum(abs(amount) for amount in booked) + abs(line.amount)
    if total > payment.amount:
        return f"adjustments of {total} would claw back more than the {payment.amount} settled"
    return ""


def _ensure_account(journal: Journal, code: str, type_: AccountType, currency: str) -> None:
    try:
        journal.account(code)
    except AccountNotFound:
        with suppress(DuplicateAccount):  # opened concurrently; the Journal uses a savepoint
            journal.open_account(NewAccount(code=code, type=type_, currency=currency))


def _banking_days(since: Any) -> Iterator[date]:
    day = new_york_date(since)
    if not is_banking_day(day):
        day = next_banking_day(day)
    while True:
        yield day
        day = next_banking_day(day)


def _line_columns(line: ReportLine) -> dict[str, Any]:
    return {
        "line_id": line.line_id,
        "type": line.type,
        "provider_payment_id": line.provider_payment_id,
        "idempotency_key": line.idempotency_key,
        "rail": line.rail,
        "direction": line.direction,
        "currency": line.currency,
        "amount": line.amount,
        "settled_at": line.settled_at,
        "original_line_id": line.original_line_id,
        "reason": line.reason,
    }


def _view(connection: Connection, report_id: int) -> SettlementReportView:
    report = connection.execute(
        select(settlement_reports).where(settlement_reports.c.id == report_id)
    ).one()
    lines = connection.execute(
        select(settlement_report_lines)
        .where(settlement_report_lines.c.report_id == report_id)
        .order_by(settlement_report_lines.c.position)
    ).all()
    adjusted = {
        row.line_id: adjustment_view(row)
        for row in connection.execute(
            adjustments_query().where(settlement_report_lines.c.report_id == report_id)
        )
    }
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
                line_id=line.line_id,
                type=line.type,
                provider_payment_id=line.provider_payment_id,
                idempotency_key=line.idempotency_key,
                rail=line.rail,
                direction=line.direction,
                currency=line.currency,
                amount=line.amount,
                settled_at=line.settled_at,
                original_line_id=line.original_line_id,
                reason=line.reason,
                adjustment=adjusted.get(line.id),
            )
            for line in lines
        ],
    )
