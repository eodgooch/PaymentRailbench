"""Reconcile one Banking Day: `python -m railbench.reconciliation reconcile <date>`.

Runs against the Ledger's database at DATABASE_URL as the Ledger's app role, on the wall
clock, and prints the day's Breaks. Reconciling a day twice finds the same Breaks
(ADR 0015); it refuses (exit 2) until the day's report and the next Banking Day's are
ingested and RECONCILIATION_SETTLE_SECONDS have passed since.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import date, timedelta

from railbench.common.clock import WallClock
from railbench.common.db import make_engine
from railbench.common.settings import Settings
from railbench.reconciliation.matching import ReportDay
from railbench.reconciliation.service import CsvReports, NotReady, Reconciliation


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m railbench.reconciliation")
    commands = parser.add_subparsers(dest="command", required=True)
    reconcile = commands.add_parser("reconcile", help="reconcile one Banking Day")
    reconcile.add_argument("day", type=date.fromisoformat)
    reconcile.add_argument("--provider", default="simulator")
    args = parser.parse_args(argv)

    settings = Settings()
    engine = make_engine(settings.database_url, role=settings.ledger_database_role or None)
    reconciliation = Reconciliation(
        engine,
        WallClock(),
        CsvReports(args.provider),
        settle=timedelta(seconds=settings.reconciliation_settle_seconds),
    )
    try:
        return _reconcile(reconciliation, ReportDay(args.provider, args.day))
    finally:
        engine.dispose()


def _reconcile(reconciliation: Reconciliation, day: ReportDay) -> int:
    try:
        ran = reconciliation.reconcile(day)
    except NotReady as error:
        print(error, file=sys.stderr)
        return 2
    print(f"{ran.provider} {ran.banking_day}: as known at {ran.known_at.isoformat()}")
    for found in reconciliation.breaks(status=None, provider=day.provider, banking_day=day.day):
        print(
            f"  {found.id} {found.break_type} {found.match_key} {found.exposure} "
            f"{found.currency} ({found.status})"
        )
    status = reconciliation.day_status(day)
    print(f"{status.open_breaks} open, exposure {status.open_exposure or 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
