"""Running Reconciliation, the Exception Queue and resolving Breaks (SPEC §7.1, ADR 0015).

Reconciling one Provider's Banking Day D waits for two ingested Settlement Reports, D's and
the next Banking Day N's (so a line one day late is a timing mismatch rather than missing
at the Provider), and matches what the Ledger knew at the moment the later of the two was
received (`known_at`): every row it reads is filtered to a Recorded Time (or receipt) at or
before that moment. Every table it reads is append-only, so the same day always gives the
same view, and the same view gives the same Breaks (`matching.match`, pure), whenever and
however often it runs: re-running it is idempotent.

One run is one database transaction under a lock on (Provider, day): the run is appended
once per (Provider, day), each Break once by its id (a hash of provider, day, Break Type and
match key), with its evidence. A re-run adds only a Break the stored run lacks, which can
only be a write that committed after the first run with a Recorded Time before `known_at`.
The `reconciliation@1.0.0` job (`reconcile_due`) runs on the Ledger's Clock after report
ingest and reconciles every day whose two reports are in.

Breaks are never edited or removed, and nothing resolves one on its own: a later run that no
longer finds a disagreement (a late Provider Event finally applied) adds nothing and leaves
the Break open. A person resolves it, once, with exactly one of a correcting Transaction
already in the Ledger (posted through the Journal by deterministic code) or a reason.

Reconciliation never reads the Fault Manifest: it may not import `railbench.scenarios`, and
the app role can't read the `simulator` schema.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Connection, Engine, Row, and_, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from railbench.common.calendar import banking_day_starts, next_banking_day
from railbench.common.clock import Clock
from railbench.common.locks import LockSpace, lock_name
from railbench.common.settlement_reports import report_day, report_published_at
from railbench.ledger.model import SystemJob
from railbench.ledger.tables import transactions
from railbench.payments.model import ReportOutcome
from railbench.payments.provider import Provider, SettlementReport
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

ReadReport = Callable[[date, bytes], SettlementReport]


class ReconciliationError(Exception):
    pass


class NotReady(ReconciliationError):
    """The day's report, or the next Banking Day's, isn't ingested yet."""


class BreakNotFound(ReconciliationError):
    pass


class ResolutionConflict(ReconciliationError):
    """The Break is already resolved, differently."""


class CorrectingTransactionNotFound(ReconciliationError):
    pass


@dataclass(frozen=True)
class _Report:
    id: int
    received_at: datetime


class Reconciliation:
    def __init__(self, engine: Engine, clock: Clock, provider: Provider) -> None:
        self._engine = engine
        self._clock = clock
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider.name

    # Running it

    def reconcile_due(self) -> list[RunView]:
        """Reconcile every Banking Day whose report and next Banking Day's report are
        ingested and that hasn't been reconciled yet, oldest first."""
        with self._engine.connect() as connection:
            ingested = self._ingested(connection)
            done = self._reconciled(connection)
        return [
            self.reconcile(day)
            for day in sorted(ingested)
            if day not in done and next_banking_day(day) in ingested
        ]

    def reconcile(self, day: date) -> RunView:
        """Reconcile the Provider's Banking Day `day`, or re-run it: the same Breaks either
        way. Raises NotReady until its report and the next Banking Day's are ingested."""
        provider = self._provider.name
        with self._engine.begin() as connection:
            lock_name(connection, LockSpace.RECONCILIATION, f"{provider}:{day.isoformat()}")
            ingested = self._ingested(connection)
            following = next_banking_day(day)
            if day not in ingested or following not in ingested:
                raise NotReady(
                    f"Reconciling {provider} {day} needs the Settlement Reports for {day} and "
                    f"{following} ingested"
                )
            report, later = ingested[day], ingested[following]
            known_at = max(report.received_at, later.received_at)
            run_id = connection.scalar(
                select(runs.c.id).where(runs.c.provider == provider, runs.c.banking_day == day)
            )
            now = self._clock.now()
            if run_id is None:
                run_id = connection.execute(
                    insert(runs)
                    .values(
                        provider=provider,
                        banking_day=day,
                        report_id=report.id,
                        next_report_id=later.id,
                        known_at=known_at,
                        job_name=RECONCILIATION_JOB.name,
                        job_version=RECONCILIATION_JOB.version,
                        recorded_at=now,
                    )
                    .returning(runs.c.id)
                ).scalar_one()
            view = _DayLoader(connection, provider, day, known_at, report.id).load(
                self._provider.read_settlement_report
            )
            for found in match(view):
                identity = break_id(provider, day, found.break_type, found.match_key)
                inserted = connection.scalar(
                    pg_insert(breaks)
                    .values(
                        id=identity,
                        run_id=run_id,
                        provider=provider,
                        banking_day=day,
                        break_type=found.break_type,
                        match_key=found.match_key,
                        exposure=found.exposure,
                        currency=found.currency,
                        facts=found.facts,
                        detected_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=[breaks.c.id])
                    .returning(breaks.c.id)
                )
                if inserted is not None:
                    _record_evidence(connection, identity, found.evidence)
            return _run_view(connection, run_id)

    def next_due(self) -> datetime | None:
        """When the next Banking Day that has anything to reconcile (a report line, or a
        settlement the Ledger expects on its report) can be reconciled, if it's still to
        come: when the next Banking Day's report is published. For a Scenario run, which
        ticks then and goes quiet afterwards."""
        provider = self._provider.name
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
        due = [report_published_at(next_banking_day(day)) for day in (with_lines | expected) - done]
        return min((moment for moment in due if moment > now), default=None)

    # The Exception Queue

    def breaks(
        self,
        status: BreakStatus | None = BreakStatus.OPEN,
        provider: str | None = None,
        banking_day: date | None = None,
        break_type: BreakType | None = None,
    ) -> list[Break]:
        """Breaks, by default the open ones (the Exception Queue), largest exposure first,
        then oldest, then by id."""
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
            return _breaks(connection, self._clock.now(), and_(True, *conditions))

    def break_(self, identity: str) -> Break:
        with self._engine.connect() as connection:
            found = _breaks(connection, self._clock.now(), breaks.c.id == identity)
        if not found:
            raise BreakNotFound(f"no Break {identity}")
        return found[0]

    def day_status(self, day: date, provider: str | None = None) -> DayStatus:
        """Whether the Provider's Banking Day is reconciled, and its unresolved Breaks:
        how many, and their total exposure per currency (for Daily Close, SPEC §3.4)."""
        provider = provider or self._provider.name
        with self._engine.connect() as connection:
            reconciled = connection.scalar(
                select(runs.c.id).where(runs.c.provider == provider, runs.c.banking_day == day)
            )
            rows = connection.execute(
                select(breaks.c.currency, breaks.c.exposure, break_resolutions.c.id)
                .outerjoin(break_resolutions, break_resolutions.c.break_id == breaks.c.id)
                .where(breaks.c.provider == provider, breaks.c.banking_day == day)
            ).all()
        open_rows = [row for row in rows if row.id is None]
        exposure: dict[str, int] = {}
        for row in open_rows:
            exposure[row.currency] = exposure.get(row.currency, 0) + row.exposure
        return DayStatus(
            provider=provider,
            banking_day=day,
            reconciled=reconciled is not None,
            breaks=len(rows),
            open_breaks=len(open_rows),
            open_exposure=dict(sorted(exposure.items())),
        )

    # Resolving a Break

    def resolve(self, identity: str, new: NewResolution) -> tuple[Break, bool]:
        """Resolve a Break, once. Returns it and whether this was a retry of the same
        resolution. Raises BreakNotFound, CorrectingTransactionNotFound, or
        ResolutionConflict for a Break already resolved otherwise."""
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
                if correcting is not None and not _exists(connection, correcting):
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
                        recorded_at=self._clock.now(),
                    )
                )
                replayed = False
            [found] = _breaks(connection, self._clock.now(), breaks.c.id == identity)
        return found, replayed

    # Reading the stored reports

    def _ingested(self, connection: Connection) -> dict[date, _Report]:
        return {
            row.report_date: _Report(row.id, row.received_at)
            for row in connection.execute(
                select(
                    settlement_reports.c.id,
                    settlement_reports.c.report_date,
                    settlement_reports.c.received_at,
                ).where(
                    settlement_reports.c.provider == self._provider.name,
                    settlement_reports.c.outcome == ReportOutcome.INGESTED,
                )
            )
        }

    def _reconciled(self, connection: Connection) -> set[date]:
        return set(
            connection.scalars(
                select(runs.c.banking_day).where(runs.c.provider == self._provider.name)
            )
        )


class _DayLoader:
    """The Ledger's view of one Provider's Banking Day as known at `known_at`."""

    def __init__(
        self, connection: Connection, provider: str, day: date, known_at: datetime, report_id: int
    ) -> None:
        self._c = connection
        self._provider = provider
        self._day = day
        self._known_at = known_at
        self._report_id = report_id

    def load(self, read_report: ReadReport) -> DayView:
        c, known_at, day = self._c, self._known_at, self._day
        next_day = next_banking_day(day)
        known_reports = {
            row.id: row.report_date
            for row in c.execute(
                select(settlement_reports.c.id, settlement_reports.c.report_date).where(
                    settlement_reports.c.provider == self._provider,
                    settlement_reports.c.outcome == ReportOutcome.INGESTED,
                    settlement_reports.c.received_at <= known_at,
                    settlement_reports.c.report_date <= next_day,
                )
            )
        }
        day_rows = c.execute(
            select(settlement_report_lines).where(
                settlement_report_lines.c.report_id == self._report_id
            )
        ).all()
        found = self._payments({row.provider_payment_id for row in day_rows})
        named = {row.provider_payment_id for row in day_rows} | set(found)
        settlement_rows = c.execute(
            select(settlement_report_lines).where(
                settlement_report_lines.c.report_id.in_(known_reports),
                settlement_report_lines.c.type == "settlement",
                settlement_report_lines.c.provider_payment_id.in_(named),
            )
        ).all()
        return DayView(
            provider=self._provider,
            day=day,
            report_id=self._report_id,
            payments=found,
            settlement_lines=tuple(
                _stated(row, known_reports[row.report_id]) for row in settlement_rows
            ),
            adjustment_lines=self._adjustments([r for r in day_rows if r.type == "adjustment"]),
            reissues=self._reissues(read_report),
        )

    def _payments(self, named: set[str]) -> dict[str, LedgerPayment]:
        """The Payments the Ledger had recorded settled around D, and those D's lines name."""
        c, known_at = self._c, self._known_at
        start = banking_day_starts(self._day) - _SETTLED_WINDOW
        end = banking_day_starts(next_banking_day(self._day)) + timedelta(days=1)
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
        rows = c.execute(
            select(payments).where(
                payments.c.provider == self._provider,
                payments.c.recorded_at <= known_at,
                payments.c.id.in_(settled_around) | payments.c.provider_payment_id.in_(named),
            )
        ).all()
        ids = [row.id for row in rows]
        steps: dict[int, list[Row[Any]]] = {}
        for step in c.execute(
            select(transitions)
            .where(transitions.c.payment_id.in_(ids), transitions.c.recorded_at <= known_at)
            .order_by(transitions.c.id)
        ):
            steps.setdefault(step.payment_id, []).append(step)
        heard: dict[int, list[int]] = {}
        for outcome in c.execute(
            select(inbox_outcomes.c.payment_id, inbox_outcomes.c.inbox_id).where(
                inbox_outcomes.c.payment_id.in_(ids), inbox_outcomes.c.recorded_at <= known_at
            )
        ):
            heard.setdefault(outcome.payment_id, []).append(outcome.inbox_id)
        found = {}
        for row in rows:
            its = steps.get(row.id, [])
            states = [PaymentState(step.state) for step in its]
            settled = next((s for s in its if s.state == PaymentState.SETTLED), None)
            found[row.provider_payment_id] = LedgerPayment(
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
                    transitions=tuple(s.id for s in its),
                    inbox=tuple(
                        {
                            *heard.get(row.id, []),
                            *(s.inbox_id for s in its if s.inbox_id is not None),
                        }
                    ),
                    transactions=tuple(
                        s.transaction_id for s in its if s.transaction_id is not None
                    ),
                ),
            )
        return found

    def _adjustments(self, rows: Iterable[Row[Any]]) -> tuple[AdjustmentLine, ...]:
        rows = list(rows)
        decided: dict[int, list[Row[Any]]] = {}
        for decision in self._c.execute(
            select(provider_adjustments)
            .where(
                provider_adjustments.c.line_id.in_([row.id for row in rows]),
                provider_adjustments.c.recorded_at <= self._known_at,
            )
            .order_by(provider_adjustments.c.id)
        ):
            decided.setdefault(decision.line_id, []).append(decision)
        found = []
        for row in rows:
            decisions = decided.get(row.id, [])
            latest = decisions[-1] if decisions else None
            found.append(
                AdjustmentLine(
                    line=_stated(row, self._day),
                    outcome=None if latest is None else latest.outcome,
                    decisions=tuple(d.id for d in decisions),
                    payment_id=None if latest is None else latest.payment_id,
                    detail="" if latest is None else latest.detail,
                )
            )
        return tuple(found)

    def _reissues(self, read_report: ReadReport) -> tuple[Reissue, ...]:
        found = []
        for row in self._c.execute(
            select(settlement_reports)
            .where(
                settlement_reports.c.provider == self._provider,
                settlement_reports.c.report_date == self._day,
                settlement_reports.c.outcome == ReportOutcome.CONFLICT,
                settlement_reports.c.received_at <= self._known_at,
            )
            .order_by(settlement_reports.c.id)
        ):
            # Kept as a conflict only once the adapter had read it, so it reads again.
            stated = read_report(self._day, bytes(row.body))
            found.append(
                Reissue(
                    report_id=row.id,
                    body_sha256=row.body_sha256,
                    lines=tuple(
                        RestatedLine(line.line_id, line.currency, line.amount)
                        for line in stated.lines
                    ),
                )
            )
        return tuple(found)


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
    rows = [
        {"break_id": identity}
        | {column: None for column in EVIDENCE_COLUMNS.values()}
        | {EVIDENCE_COLUMNS[kind]: ref}
        for kind in EVIDENCE_KINDS
        for ref in getattr(evidence, kind)
    ]
    if rows:
        connection.execute(insert(break_evidence), rows)


def _run_view(connection: Connection, run_id: int) -> RunView:
    run = connection.execute(select(runs).where(runs.c.id == run_id)).one()
    found = connection.scalars(
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
        job=f"{run.job_name}@{run.job_version}",
        recorded_at=run.recorded_at,
        breaks=list(found),
    )


def _resolution(row: Row[Any]) -> Resolution:
    return Resolution(
        actor=Resolver(operator_id=row.actor_operator_id, proposal_id=row.actor_proposal_id),
        correcting_transaction_id=row.correcting_transaction_id,
        reason=row.reason,
        recorded_at=row.recorded_at,
    )


def _breaks(connection: Connection, now: datetime, where: Any) -> list[Break]:
    rows = connection.execute(
        select(breaks, break_resolutions.c.id.label("resolution_id"))
        .outerjoin(break_resolutions, break_resolutions.c.break_id == breaks.c.id)
        .where(where)
        .order_by(breaks.c.exposure.desc(), breaks.c.detected_at, breaks.c.id)
    ).all()
    ids = [row.id for row in rows]
    evidence: dict[str, dict[str, list[int]]] = {}
    for item in connection.execute(
        select(break_evidence)
        .where(break_evidence.c.break_id.in_(ids))
        .order_by(break_evidence.c.id)
    ):
        kinds = evidence.setdefault(item.break_id, {})
        for kind, column in EVIDENCE_COLUMNS.items():
            if getattr(item, column) is not None:
                kinds.setdefault(kind, []).append(getattr(item, column))
    resolved = {
        row.break_id: _resolution(row)
        for row in connection.execute(
            select(break_resolutions).where(break_resolutions.c.break_id.in_(ids))
        )
    }
    found = []
    for row in rows:
        resolution = resolved.get(row.id)
        until = now if resolution is None else resolution.recorded_at
        found.append(
            Break(
                id=row.id,
                provider=row.provider,
                banking_day=row.banking_day,
                break_type=BreakType(row.break_type),
                match_key=row.match_key,
                exposure=row.exposure,
                currency=row.currency,
                facts=row.facts,
                evidence=EvidenceView(**evidence.get(row.id, {})),
                detected_at=row.detected_at,
                status=BreakStatus.OPEN if resolution is None else BreakStatus.RESOLVED,
                age_seconds=max(0, int((until - row.detected_at).total_seconds())),
                resolution=resolution,
            )
        )
    return found


def _exists(connection: Connection, transaction_id: int) -> bool:
    found = connection.scalar(select(transactions.c.id).where(transactions.c.id == transaction_id))
    return found is not None
