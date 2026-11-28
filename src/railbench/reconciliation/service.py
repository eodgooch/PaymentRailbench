"""Running Reconciliation, the Exception Queue and resolving Breaks (SPEC §7.1, ADR 0015).

Reconciling one Provider's Banking Day D waits for two ingested Settlement Reports, D's and
the next Banking Day N's (so a line one day late is a timing mismatch rather than missing
at the Provider), and matches what the Ledger knew at the moment the later of the two was
received (`known_at`): every row it reads is filtered to a Recorded Time (or receipt) at or
before that moment. Every table it reads is append-only, so the same day gives the same
view, and the same view gives the same Breaks (`matching.match`, pure).

One run is one database transaction under a lock on (Provider, day): the run is appended
once per (Provider, day), each Break once by its id (a hash of provider, day, Break Type and
match key), with its evidence. A run waits until the Clock is `settle` past `known_at`
(a setting, a few minutes in a running service, 0 on a Scenario's ManualClock), so a write
that read the Clock before `known_at` but committed a little later is in it; a re-run adds
only a Break the stored run lacks, which only a write slower than that margin could cause.
The `reconciliation@1.0.0` job (`reconcile_due`) runs on the Ledger's Clock after report
ingest and reconciles every day that's ready.

Breaks are never edited or removed, and nothing resolves one on its own: a later run that no
longer finds a disagreement (a late Provider Event finally applied) adds nothing and leaves
the Break open. A person resolves it, once, with exactly one of a correcting Transaction
already in the Ledger (posted through the Journal by deterministic code) or a reason.

Reconciliation reads reports only through a `ReportReader` (a Provider's name and how it
reads its own reports), and never reads the Fault Manifest: it may not import
`railbench.scenarios`, and the app role can't read the `simulator` schema.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    Row,
    and_,
    insert,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from railbench.common.calendar import banking_day_starts, next_banking_day
from railbench.common.clock import Clock
from railbench.common.locks import LockSpace, lock_name
from railbench.common.settlement_reports import report_day, report_published_at
from railbench.ledger.model import SystemJob, job_label
from railbench.ledger.tables import transactions
from railbench.payments.model import AdjustmentOutcome, ReportOutcome
from railbench.payments.provider import LineType, SettlementReport, read_report_csv
from railbench.payments.state_machine import PaymentState, current_state
from railbench.payments.tables import (
    inbox_outcomes,
    payments,
    provider_adjustments,
    settlement_report_lines,
    settlement_reports,
    transitions,
)
from railbench.reconciliation.matching import (
    EVIDENCE_KINDS,
    AdjustmentLine,
    BreakType,
    DayView,
    Evidence,
    LedgerPayment,
    Reissue,
    ReportDay,
    RestatedLine,
    StatedLine,
    break_id,
    match,
)
from railbench.reconciliation.model import (
    Break,
    BreakStatus,
    DayStatus,
    EvidenceView,
    NewResolution,
    Resolution,
    Resolver,
    RunView,
)
from railbench.reconciliation.tables import (
    EVIDENCE_COLUMNS,
    break_evidence,
    break_resolutions,
    breaks,
    runs,
)

RECONCILIATION_JOB = SystemJob(name="reconciliation", version="1.0.0")
# How far before D a settlement can happen and still count toward D's report: a weekend
# and a Fed holiday at most, so a week is plenty.
_SETTLED_WINDOW = timedelta(days=7)


class ReportReader(Protocol):
    """What Reconciliation needs of a Provider: its name, and how to read one of its
    Settlement Reports (a re-issue kept raw). Every Provider adapter is one."""

    @property
    def name(self) -> str: ...

    def read_settlement_report(self, report_date: date, body: bytes) -> SettlementReport: ...


@dataclass(frozen=True)
class CsvReports:
    """A Provider whose reports are in the shared CSV format (ADR 0014), read offline."""

    name: str

    def read_settlement_report(self, report_date: date, body: bytes) -> SettlementReport:
        return read_report_csv(self.name, report_date, body)


class ReconciliationError(Exception):
    pass


class UnknownProvider(ReconciliationError):
    pass


class NotReady(ReconciliationError):
    """The day's report, or the next Banking Day's, isn't ingested yet, or the run would
    start before the settle margin after the later one."""


class BreakNotFound(ReconciliationError):
    pass


class ResolutionConflict(ReconciliationError):
    """The Break is already resolved, differently."""


class CorrectingTransactionNotFound(ReconciliationError):
    pass


@dataclass(frozen=True)
class _IngestedReport:
    id: int
    received_at: datetime


class Reconciliation:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        reports: ReportReader,
        settle: timedelta = timedelta(0),
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._reports = reports
        self._settle = settle

    @property
    def provider(self) -> str:
        return self._reports.name

    # Running it

    def reconcile_due(self) -> list[RunView]:
        """Reconcile every Banking Day that's ready (its report and the next Banking Day's
        ingested, the settle margin past) and not reconciled yet, oldest first."""
        with self._engine.connect() as connection:
            ingested = self._ingested(connection)
            done = self._reconciled(connection)
        now = self._clock.now()
        ready = [
            ReportDay(self.provider, day)
            for day in sorted(ingested)
            if day not in done
            and (later := ingested.get(next_banking_day(day))) is not None
            and max(ingested[day].received_at, later.received_at) + self._settle <= now
        ]
        return [self.reconcile(day) for day in ready]

    def reconcile(self, report_day_: ReportDay) -> RunView:
        """Reconcile one Provider's Banking Day, or re-run it: the same Breaks either way.
        Raises UnknownProvider, or NotReady until it's ready."""
        self._check(report_day_)
        now = self._clock.now()
        with self._engine.begin() as connection:
            lock_name(connection, LockSpace.RECONCILIATION, str(report_day_))
            ingested = self._ingested(connection)
            following = report_day_.next
            report, later = ingested.get(report_day_.day), ingested.get(following.day)
            if report is None or later is None:
                raise NotReady(
                    f"Reconciling {report_day_} needs the Settlement Reports for "
                    f"{report_day_.day} and {following.day} ingested"
                )
            known_at = max(report.received_at, later.received_at)
            if now < known_at + self._settle:
                raise NotReady(f"Reconciling {report_day_} waits until {known_at + self._settle}")
            run_id = _run_id(connection, report_day_)
            if run_id is None:
                run_id = connection.execute(
                    insert(runs)
                    .values(
                        provider=report_day_.provider,
                        banking_day=report_day_.day,
                        report_id=report.id,
                        next_report_id=later.id,
                        known_at=known_at,
                        job_name=RECONCILIATION_JOB.name,
                        job_version=RECONCILIATION_JOB.version,
                        recorded_at=now,
                    )
                    .returning(runs.c.id)
                ).scalar_one()
            view = _DayLoader(connection, report_day_, known_at, report.id).load(self._reports)
            for disagreement in match(view):
                identity = break_id(report_day_, disagreement.break_type, disagreement.match_key)
                inserted = connection.scalar(
                    pg_insert(breaks)
                    .values(
                        id=identity,
                        run_id=run_id,
                        provider=report_day_.provider,
                        banking_day=report_day_.day,
                        break_type=disagreement.break_type,
                        match_key=disagreement.match_key,
                        exposure=disagreement.exposure,
                        currency=disagreement.currency,
                        facts=disagreement.facts,
                        detected_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=[breaks.c.id])
                    .returning(breaks.c.id)
                )
                if inserted is not None:
                    _record_evidence(connection, identity, disagreement.evidence)
            return _run_view(connection, run_id)

    def next_due(self) -> datetime | None:
        """When the next Banking Day that has anything to reconcile (a report line, or a
        settlement the Ledger expects on its report) can be reconciled, if it's still to
        come: when the next Banking Day's report is published, plus the settle margin. For a
        Scenario run, which ticks then and goes quiet afterwards."""
        provider = self.provider
        now = self._clock.now()
        with self._engine.connect() as connection:
            done = self._reconciled(connection)
            with_lines = set(
                connection.scalars(
                    select(settlement_reports.c.report_date)
                    .join(
                        settlement_report_lines,
                        settlement_report_lines.c.report_id == settlement_reports.c.id,
                    )
                    .where(
                        settlement_reports.c.provider == provider,
                        settlement_reports.c.outcome == ReportOutcome.INGESTED,
                    )
                )
            )
            expected = {
                report_day(provider, row.rail, row.effective_at)
                for row in connection.execute(
                    select(payments.c.rail, transitions.c.effective_at)
                    .join(transitions, transitions.c.payment_id == payments.c.id)
                    .where(
                        payments.c.provider == provider,
                        transitions.c.state == PaymentState.SETTLED,
                    )
                )
            }
        due = [
            report_published_at(next_banking_day(day)) + self._settle
            for day in (with_lines | expected) - done
        ]
        return min((moment for moment in due if moment > now), default=None)

    # The Exception Queue

    def breaks(
        self,
        status: BreakStatus | None = BreakStatus.OPEN,
        provider: str | None = None,
        banking_day: date | None = None,
        break_type: BreakType | None = None,
    ) -> list[Break]:
        """Breaks, by default the open ones (the Exception Queue): within each currency,
        largest exposure first, then oldest (by the Banking Day they concern), then by id."""
        conditions: list[ColumnElement[bool]] = []
        if status is BreakStatus.OPEN:
            conditions.append(break_resolutions.c.id.is_(None))
        elif status is BreakStatus.RESOLVED:
            conditions.append(break_resolutions.c.id.is_not(None))
        if provider is not None:
            conditions.append(breaks.c.provider == provider)
        if banking_day is not None:
            conditions.append(breaks.c.banking_day == banking_day)
        if break_type is not None:
            conditions.append(breaks.c.break_type == break_type)
        with self._engine.connect() as connection:
            return read_breaks(connection, self._clock.now(), and_(true(), *conditions))

    def find(self, identity: str) -> Break:
        with self._engine.connect() as connection:
            found = read_breaks(connection, self._clock.now(), breaks.c.id == identity)
        if not found:
            raise BreakNotFound(f"no Break {identity}")
        return found[0]

    def day_status(self, report_day_: ReportDay) -> DayStatus:
        """Whether the Provider's Banking Day is reconciled, and its unresolved Breaks:
        how many, and their total exposure per currency (for Daily Close, SPEC §3.4)."""
        self._check(report_day_)
        with self._engine.connect() as connection:
            reconciled = _run_id(connection, report_day_) is not None
            rows = connection.execute(
                select(breaks.c.currency, breaks.c.exposure, break_resolutions.c.id)
                .outerjoin(break_resolutions, break_resolutions.c.break_id == breaks.c.id)
                .where(
                    breaks.c.provider == report_day_.provider,
                    breaks.c.banking_day == report_day_.day,
                )
            ).all()
        unresolved = [row for row in rows if row.id is None]
        exposure: dict[str, int] = {}
        for row in unresolved:
            exposure[row.currency] = exposure.get(row.currency, 0) + row.exposure
        return DayStatus(
            provider=report_day_.provider,
            banking_day=report_day_.day,
            reconciled=reconciled,
            breaks=len(rows),
            open_breaks=len(unresolved),
            open_exposure=dict(sorted(exposure.items())),
        )

    # Resolving a Break

    def resolve(self, identity: str, new: NewResolution) -> tuple[Break, bool]:
        """Resolve a Break, once. Returns it and whether this was a retry of the same
        resolution. Raises BreakNotFound, CorrectingTransactionNotFound, or
        ResolutionConflict for a Break already resolved otherwise."""
        now = self._clock.now()
        with self._engine.begin() as connection:
            lock_name(connection, LockSpace.BREAK, identity)
            if connection.scalar(select(breaks.c.id).where(breaks.c.id == identity)) is None:
                raise BreakNotFound(f"no Break {identity}")
            existing = connection.execute(
                select(break_resolutions).where(break_resolutions.c.break_id == identity)
            ).one_or_none()
            if existing is not None:
                if _resolution(existing).model_dump(exclude={"recorded_at"}) != new.model_dump():
                    raise ResolutionConflict(f"Break {identity} is already resolved otherwise")
                replayed = True
            else:
                correcting = new.correcting_transaction_id
                if correcting is not None and not _transaction_exists(connection, correcting):
                    raise CorrectingTransactionNotFound(
                        f"no Transaction {correcting} in the Ledger"
                    )
                connection.execute(
                    insert(break_resolutions).values(
                        break_id=identity,
                        actor_operator_id=new.actor.operator_id,
                        actor_proposal_id=new.actor.proposal_id,
                        correcting_transaction_id=correcting,
                        reason=new.reason,
                        recorded_at=now,
                    )
                )
                replayed = False
            [resolved] = read_breaks(connection, now, breaks.c.id == identity)
        return resolved, replayed

    # Reading the stored reports

    def _check(self, report_day_: ReportDay) -> None:
        if report_day_.provider != self.provider:
            raise UnknownProvider(f"no Provider {report_day_.provider}")

    def _ingested(self, connection: Connection) -> dict[date, _IngestedReport]:
        return {
            row.report_date: _IngestedReport(row.id, row.received_at)
            for row in connection.execute(
                select(
                    settlement_reports.c.id,
                    settlement_reports.c.report_date,
                    settlement_reports.c.received_at,
                ).where(
                    settlement_reports.c.provider == self.provider,
                    settlement_reports.c.outcome == ReportOutcome.INGESTED,
                )
            )
        }

    def _reconciled(self, connection: Connection) -> set[date]:
        return set(
            connection.scalars(select(runs.c.banking_day).where(runs.c.provider == self.provider))
        )


class _DayLoader:
    """The Ledger's view of one Provider's Banking Day as known at `known_at`."""

    def __init__(
        self, connection: Connection, report_day_: ReportDay, known_at: datetime, report_id: int
    ) -> None:
        self._connection = connection
        self._report_day = report_day_
        self._known_at = known_at
        self._report_id = report_id

    def load(self, reports: ReportReader) -> DayView:
        connection, known_at = self._connection, self._known_at
        provider = self._report_day.provider
        known_reports = {
            row.id: row.report_date
            for row in connection.execute(
                select(settlement_reports.c.id, settlement_reports.c.report_date).where(
                    settlement_reports.c.provider == provider,
                    settlement_reports.c.outcome == ReportOutcome.INGESTED,
                    settlement_reports.c.received_at <= known_at,
                    settlement_reports.c.report_date <= self._report_day.next.day,
                )
            )
        }
        day_rows = connection.execute(
            select(settlement_report_lines).where(
                settlement_report_lines.c.report_id == self._report_id
            )
        ).all()
        named = {row.provider_payment_id for row in day_rows}
        ledger_payments = self._payments(named)
        settlement_rows = connection.execute(
            select(settlement_report_lines).where(
                settlement_report_lines.c.report_id.in_(known_reports),
                settlement_report_lines.c.type == _SETTLEMENT,
                settlement_report_lines.c.provider_payment_id.in_(named | set(ledger_payments)),
            )
        ).all()
        return DayView(
            report_day=self._report_day,
            report_id=self._report_id,
            payments=ledger_payments,
            settlement_lines=tuple(
                _stated(row, known_reports[row.report_id]) for row in settlement_rows
            ),
            adjustment_lines=self._adjustments([r for r in day_rows if r.type == _ADJUSTMENT]),
            reissues=self._reissues(reports),
        )

    def _payments(self, named: set[str]) -> dict[str, LedgerPayment]:
        """The Payments the Ledger had recorded settled around D, and those D's lines name."""
        connection, known_at, day = self._connection, self._known_at, self._report_day.day
        start = banking_day_starts(day) - _SETTLED_WINDOW
        end = banking_day_starts(next_banking_day(day)) + timedelta(days=1)
        settled_around = (
            select(transitions.c.payment_id)
            .where(
                transitions.c.state == PaymentState.SETTLED,
                transitions.c.recorded_at <= known_at,
                transitions.c.effective_at >= start,
                transitions.c.effective_at < end,
            )
            .scalar_subquery()
        )
        rows = connection.execute(
            select(payments).where(
                payments.c.provider == self._report_day.provider,
                payments.c.recorded_at <= known_at,
                payments.c.id.in_(settled_around) | payments.c.provider_payment_id.in_(named),
            )
        ).all()
        ids = [row.id for row in rows]
        steps_of: dict[int, list[Row[Any]]] = {}
        for step in connection.execute(
            select(transitions)
            .where(transitions.c.payment_id.in_(ids), transitions.c.recorded_at <= known_at)
            .order_by(transitions.c.id)
        ):
            steps_of.setdefault(step.payment_id, []).append(step)
        heard: dict[int, list[int]] = {}
        for outcome in connection.execute(
            select(inbox_outcomes.c.payment_id, inbox_outcomes.c.inbox_id).where(
                inbox_outcomes.c.payment_id.in_(ids), inbox_outcomes.c.recorded_at <= known_at
            )
        ):
            heard.setdefault(outcome.payment_id, []).append(outcome.inbox_id)
        return {
            row.provider_payment_id: _payment(row, steps_of.get(row.id, []), heard) for row in rows
        }

    def _adjustments(self, rows: Iterable[Row[Any]]) -> tuple[AdjustmentLine, ...]:
        rows = list(rows)
        decided: dict[int, list[Row[Any]]] = {}
        for decision in self._connection.execute(
            select(provider_adjustments)
            .where(
                provider_adjustments.c.line_id.in_([row.id for row in rows]),
                provider_adjustments.c.recorded_at <= self._known_at,
            )
            .order_by(provider_adjustments.c.id)
        ):
            decided.setdefault(decision.line_id, []).append(decision)
        lines = []
        for row in rows:
            decisions = decided.get(row.id, [])
            latest = decisions[-1] if decisions else None
            lines.append(
                AdjustmentLine(
                    line=_stated(row, self._report_day.day),
                    outcome=None if latest is None else AdjustmentOutcome(latest.outcome),
                    decisions=tuple(d.id for d in decisions),
                    payment_id=None if latest is None else latest.payment_id,
                    detail="" if latest is None else latest.detail,
                )
            )
        return tuple(lines)

    def _reissues(self, reports: ReportReader) -> tuple[Reissue, ...]:
        reissues = []
        for row in self._connection.execute(
            select(settlement_reports)
            .where(
                settlement_reports.c.provider == self._report_day.provider,
                settlement_reports.c.report_date == self._report_day.day,
                settlement_reports.c.outcome == ReportOutcome.CONFLICT,
                settlement_reports.c.received_at <= self._known_at,
            )
            .order_by(settlement_reports.c.id)
        ):
            # Kept as a conflict only once the adapter had read it, so it reads again.
            stated = reports.read_settlement_report(self._report_day.day, bytes(row.body))
            reissues.append(
                Reissue(
                    report_id=row.id,
                    body_sha256=row.body_sha256,
                    lines=tuple(
                        RestatedLine(line.line_id, line.currency, line.amount)
                        for line in stated.lines
                    ),
                )
            )
        return tuple(reissues)


_SETTLEMENT: LineType = "settlement"
_ADJUSTMENT: LineType = "adjustment"


def _payment(row: Row[Any], steps: list[Row[Any]], heard: dict[int, list[int]]) -> LedgerPayment:
    states = [PaymentState(step.state) for step in steps]
    settled = next((s for s in steps if s.state == PaymentState.SETTLED), None)
    return LedgerPayment(
        id=row.id,
        provider_payment_id=row.provider_payment_id,
        idempotency_key=row.idempotency_key,
        rail=row.rail,
        direction=row.direction,
        currency=row.currency,
        amount=row.amount,
        state=current_state(states) if states else PaymentState.CREATED,
        settled_at=None if settled is None else settled.effective_at,
        evidence=Evidence(
            payments=(row.id,),
            transitions=tuple(s.id for s in steps),
            inbox=tuple(
                {*heard.get(row.id, []), *(s.inbox_id for s in steps if s.inbox_id is not None)}
            ),
            transactions=tuple(s.transaction_id for s in steps if s.transaction_id is not None),
        ),
    )


def _stated(row: Row[Any], report_date: date) -> StatedLine:
    return StatedLine(
        id=row.id,
        report_id=row.report_id,
        report_date=report_date,
        position=row.position,
        line_id=row.line_id,
        type=row.type,
        provider_payment_id=row.provider_payment_id,
        idempotency_key=row.idempotency_key,
        rail=row.rail,
        direction=row.direction,
        currency=row.currency,
        amount=row.amount,
        settled_at=row.settled_at,
    )


def _record_evidence(connection: Connection, identity: str, evidence: Evidence) -> None:
    empty = dict.fromkeys(EVIDENCE_COLUMNS.values())
    rows = [
        {"break_id": identity, **empty, EVIDENCE_COLUMNS[kind]: ref}
        for kind in EVIDENCE_KINDS
        for ref in getattr(evidence, kind)
    ]
    if rows:
        connection.execute(insert(break_evidence), rows)


def _run_id(connection: Connection, report_day_: ReportDay) -> int | None:
    run_id: int | None = connection.scalar(
        select(runs.c.id).where(
            runs.c.provider == report_day_.provider, runs.c.banking_day == report_day_.day
        )
    )
    return run_id


def _run_view(connection: Connection, run_id: int) -> RunView:
    run = connection.execute(select(runs).where(runs.c.id == run_id)).one()
    break_ids = connection.scalars(
        select(breaks.c.id)
        .where(breaks.c.run_id == run_id)
        .order_by(breaks.c.break_type, breaks.c.match_key)
    ).all()
    return RunView(
        provider=run.provider,
        banking_day=run.banking_day,
        report_id=run.report_id,
        next_report_id=run.next_report_id,
        known_at=run.known_at,
        job=job_label(run.job_name, run.job_version),
        recorded_at=run.recorded_at,
        breaks=list(break_ids),
    )


def _resolution(row: Row[Any]) -> Resolution:
    return Resolution(
        actor=Resolver(operator_id=row.actor_operator_id, proposal_id=row.actor_proposal_id),
        correcting_transaction_id=row.correcting_transaction_id,
        reason=row.reason,
        recorded_at=row.recorded_at,
    )


def _transaction_exists(connection: Connection, transaction_id: int) -> bool:
    found = connection.scalar(select(transactions.c.id).where(transactions.c.id == transaction_id))
    return found is not None


def concerns_since(banking_day: date) -> datetime:
    """When a Break about this Banking Day began to matter, the moment its age counts from:
    when the day's report was due (ADR 0014's 20:00 New York)."""
    return report_published_at(banking_day)


def evidence_of(connection: Connection, identities: Iterable[str]) -> dict[str, EvidenceView]:
    """Each Break's evidence, by kind, as the row ids it rests on."""
    found: dict[str, dict[str, list[int]]] = {}
    for item in connection.execute(
        select(break_evidence)
        .where(break_evidence.c.break_id.in_(list(identities)))
        .order_by(break_evidence.c.id)
    ):
        for kind, column in EVIDENCE_COLUMNS.items():
            ref = getattr(item, column)
            if ref is not None:
                found.setdefault(item.break_id, {}).setdefault(kind, []).append(ref)
    return {identity: EvidenceView(**kinds) for identity, kinds in found.items()}


def read_breaks(connection: Connection, now: datetime | None, where: Any) -> list[Break]:
    """The Breaks matching `where`, each with its evidence, resolution and related Breaks
    (those it names, and those that name it), ordered as the Exception Queue: within each
    currency, largest exposure first, then oldest, then by id. `now` gives their age (None:
    no age is wanted, and it reads 0)."""
    rows = connection.execute(
        select(breaks, break_resolutions.c.id.label("resolution_id"))
        .outerjoin(break_resolutions, break_resolutions.c.break_id == breaks.c.id)
        .where(where)
        .order_by(breaks.c.currency, breaks.c.exposure.desc(), breaks.c.banking_day, breaks.c.id)
    ).all()
    ids = [row.id for row in rows]
    evidence = evidence_of(connection, ids)
    resolved = {
        row.break_id: _resolution(row)
        for row in connection.execute(
            select(break_resolutions).where(break_resolutions.c.break_id.in_(ids))
        )
    }
    related: dict[str, set[str]] = {}
    for row in connection.execute(
        select(breaks.c.id, breaks.c.facts["related_breaks"].label("names")).where(
            breaks.c.facts.has_key("related_breaks")
        )
    ):
        for other in row.names:
            related.setdefault(row.id, set()).add(other)
            related.setdefault(other, set()).add(row.id)
    views = []
    for row in rows:
        resolution = resolved.get(row.id)
        until = now if resolution is None or now is None else min(now, resolution.recorded_at)
        age = 0 if until is None else (until - concerns_since(row.banking_day)).total_seconds()
        views.append(
            Break(
                id=row.id,
                provider=row.provider,
                banking_day=row.banking_day,
                break_type=BreakType(row.break_type),
                match_key=row.match_key,
                exposure=row.exposure,
                currency=row.currency,
                facts=row.facts,
                evidence=evidence.get(row.id, EvidenceView()),
                related_breaks=sorted(related.get(row.id, set())),
                detected_at=row.detected_at,
                status=BreakStatus.OPEN if resolution is None else BreakStatus.RESOLVED,
                age_seconds=max(0, int(age)),
                resolution=resolution,
            )
        )
    return views
