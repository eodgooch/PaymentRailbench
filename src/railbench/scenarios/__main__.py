"""Run one Scenario: `python -m railbench.scenarios run scenarios/<name>.yaml [--seed N]`.

The run gets a throwaway database on the Postgres at DATABASE_URL (`make dev` starts one),
migrated, used and dropped, with the Ledger and the Simulator in-process on one ManualClock.
It writes the Fault Manifest (default reports/manifests/<scenario>-<seed>.json) and prints
each Payment's states and Inbox outcomes, each Auction Close's Payouts, each Settlement
Report the Ledger received, and the Breaks Reconciliation found.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from railbench.common.settings import Settings
from railbench.scenarios.model import load
from railbench.scenarios.runner import fresh_database, run


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m railbench.scenarios")
    commands = parser.add_subparsers(dest="command", required=True)
    run_command = commands.add_parser("run", help="run one Scenario file")
    run_command.add_argument("scenario", type=Path)
    run_command.add_argument("--seed", type=int, default=None, help="default: the file's seed")
    run_command.add_argument("--manifest", type=Path, default=None)
    run_command.add_argument(
        "--database-url", default=None, help="a Postgres server to make a throwaway database on"
    )
    args = parser.parse_args(argv)

    scenario = load(args.scenario)
    with fresh_database(args.database_url or Settings().database_url) as url:
        result = run(scenario, url, args.seed, args.manifest)

    print(f"Scenario {scenario.name}, seed {result.seed}: {len(result.ticks)} ticks")
    for key, payment in result.summary["payments"].items():
        steps = " → ".join(step["state"] for step in payment["transitions"])
        seen = ", ".join(outcome["outcome"] for outcome in payment["inbox_outcomes"])
        print(f"  {key}: {steps} (Inbox: {seen})")
    for auction_id, close in result.summary["auction_closes"].items():
        print(f"Auction Close {auction_id}: {len(close['lots'])} lots")
        for seller, payout in close["payouts"].items():
            steps = " → ".join(step["step"] for step in payout["steps"])
            print(f"  Payout to {seller}: {payout['amount']} ({payout['requested']}): {steps}")
    for day, versions in result.summary["settlement_reports"].items():
        found = ", ".join(f"{v['outcome']} ({len(v['lines'])} lines)" for v in versions)
        print(f"Settlement Report {day}: {found}")
    reconciled = result.summary["reconciliation"]
    print(f"Reconciled {len(reconciled['runs'])} Banking Days: {len(reconciled['breaks'])} Breaks")
    for found in sorted(reconciled["breaks"].values(), key=lambda b: (b["run"], -b["exposure"])):
        print(
            f"  {found['run']} {found['break_type']} {found['match_key']}: "
            f"{found['exposure']} {found['currency']}"
        )
    print(f"{len(result.manifest.faults)} Faults injected; manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
