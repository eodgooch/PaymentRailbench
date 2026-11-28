"""Reconcile one Banking Day: `python -m railbench.reconciliation reconcile <date>`.

Runs against the Ledger's database at DATABASE_URL as the Ledger's app role, on the wall
clock, and prints the day's Breaks. Reconciling a day twice finds the same Breaks
(ADR 0015); it refuses (exit 2) until the day's report and the next Banking Day's are
ingested.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import date

from railbench.common.clock import WallClock
from railbench.common.db import make_engine
from railbench.common.settings import Settings
from railbench.payments.provider import SimulatorProvider
from railbench.reconciliation.service import NotReady, Reconciliation


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m railbench.reconciliation")
    commands = parser.add_subparsers(dest="command", required=True)
    reconcile = commands.add_parser("reconcile", help="reconcile one Banking Day")
    reconcile.add_argument("day", type=date.fromisoformat)
    args = parser.parse_args(argv)

    settings = Settings()
    engine = make_engine(settings.database_url, role=settings.ledger_database_role or None)
    # The adapter is only used to read re-issued reports already stored; it calls nobody.
    provider = SimulatorProvider(
        settings.simulator_url, settings.webhook_signing_secret.get_secret_value()
    )
    reconciliation = Reconciliation(engine, WallClock(), provider)
    try:
        return _reconcile(reconciliation, args.day)
    finally:
        engine.dispose()


def _reconcile(reconciliation: Reconciliation, day: date) -> int:
    try:
        ran = reconciliation.reconcile(day)
    except NotReady as error:
        print(error, file=sys.stderr)
        return 2
    print(f"{ran.provider} {ran.banking_day}: as known at {ran.known_at.isoformat()}")
    for found in reconciliation.breaks(status=None, banking_day=day):
        print(
            f"  {found.id} {found.break_type} {found.match_key} {found.exposure} "
            f"{found.currency} ({found.status})"
        )
    status = reconciliation.day_status(day)
    print(f"{status.open_breaks} open, exposure {status.open_exposure or 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
