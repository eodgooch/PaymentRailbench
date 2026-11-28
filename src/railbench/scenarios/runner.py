"""Run one Scenario against the Ledger and the Simulator (SPEC §6, ADR 0011).

Both services run in-process on one ManualClock and talk over real localhost HTTP, exactly
as in the end-to-end tests: the Ledger creates Payments at the Simulator through the
Provider interface and receives signed webhooks, Faults included. The runner plays the part
of Mintwharf's buyers and of time:

1. open each Customer's Customer Balance at the start;
2. hand the Simulator its plan for every Payment (outcome and Faults, resolved from the
   seed), the way a test would configure a sandbox;
3. originate each Payment through the Ledger API at its time;
4. move the Clock straight to the next moment anything is due (a Payment to originate, a
   Payment to settle or end, a webhook to deliver, ACH funds due to become Available),
   tick the Simulator and then the Ledger's availability job, and repeat until nothing is
   left to do.

No randomness is drawn here and no wall clock is read, so a Scenario and seed give the same
Ledger, event IDs, delivery order and fault manifest every run. The database must be
migrated and hold no other Scenario's Accounts (`fresh_database()` makes one).
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text

from railbench.common.clock import ManualClock
from railbench.common.db import LEDGER_APP_ROLE, make_engine
from railbench.common.settings import Settings
from railbench.ledger.app import create_app as create_ledger_app
from railbench.ledger.journal import Journal
from railbench.ledger.model import AccountType, NewAccount
from railbench.payments.service import Payments
from railbench.scenarios.manifest import Manifest, manifest_for
from railbench.scenarios.model import Scenario
from railbench.scenarios.servers import free_port, serving
from railbench.scenarios.summary import Summary, ledger_summary
from railbench.simulator.app import create_app as create_simulator_app
from railbench.simulator.provider import Simulator

ALEMBIC_INI = Path(__file__).parents[3] / "alembic.ini"
# A run that hasn't gone quiet after this many ticks is stuck (a bug, not a long Scenario).
MAX_TICKS = 10_000
SIGNING_SECRET = "scenario-run-secret"  # in-process only: both ends are this process


class RunDidNotSettle(RuntimeError):
    pass


@dataclass(frozen=True)
class Run:
    scenario: Scenario
    seed: int
    manifest: Manifest
    # Every moment the Ledger's availability job ran, in order: with the Inbox, what a
    # replay needs to reproduce the Ledger's Recorded Times.
    ticks: list[datetime]
    # Scenario Payment id → the Ledger's Payment id.
    payments: dict[str, int]
    summary: Summary = field(repr=False)


def run(scenario: Scenario, database_url: str, seed: int | None = None) -> Run:
    """Run `scenario` with `seed` (default: the Scenario's own) against the migrated, empty
    database at `database_url`."""
    seed = scenario.seed if seed is None else seed
    plans = scenario.plans(seed)
    clock = ManualClock(scenario.start)
    owner = create_engine(database_url)
    app_engine = make_engine(database_url, role=LEDGER_APP_ROLE)
    ledger_port, simulator_port = free_port(), free_port()
    ledger_url = f"http://127.0.0.1:{ledger_port}"
    settings = Settings(
        simulator_url=f"http://127.0.0.1:{simulator_port}",
        ledger_webhook_url=f"{ledger_url}/providers/simulator/events",
        webhook_signing_secret=SIGNING_SECRET,
    )
    ledger_app = create_ledger_app(app_engine, clock, settings)
    simulator_app = create_simulator_app(owner, clock, settings)
    journal: Journal = ledger_app.state.journal
    payments: Payments = ledger_app.state.payments
    simulator: Simulator = simulator_app.state.simulator
    try:
        with (
            serving(simulator_app, simulator_port),
            serving(ledger_app, ledger_port),
            httpx.Client(base_url=ledger_url, timeout=10.0) as ledger,
        ):
            for customer in scenario.customers:
                currencies = sorted(
                    {p.currency for p in scenario.payments if p.customer == customer.id}
                )
                for currency in currencies:
                    journal.open_account(
                        NewAccount(
                            code=customer.account(currency),
                            type=AccountType.LIABILITY,
                            currency=currency,
                        )
                    )
            for plan in plans.values():
                simulator.plan(plan)

            waiting = sorted(scenario.payments, key=lambda p: p.at)  # stable: file order ties
            originated: dict[str, int] = {}
            ticks: list[datetime] = []
            while True:
                now = clock.now()
                while waiting and scenario.start + waiting[0].at <= now:
                    payment = waiting.pop(0)
                    response = ledger.post(
                        "/payments",
                        json={
                            "idempotency_key": plans[payment.id].idempotency_key,
                            "rail": "ach",
                            "direction": "pull",
                            "customer_account": scenario.customer(payment.customer).account(
                                payment.currency
                            ),
                            "amount": payment.amount,
                            "currency": payment.currency,
                            "description": payment.description,
                        },
                    )
                    response.raise_for_status()
                    originated[payment.id] = response.json()["id"]
                simulator.tick()
                payments.make_available()
                ticks.append(now)
                if len(ticks) > MAX_TICKS:
                    raise RunDidNotSettle(f"{scenario.name} still busy after {MAX_TICKS} ticks")
                due = [
                    moment
                    for moment in (
                        simulator.next_due(),
                        waiting[0].at + scenario.start if waiting else None,
                        _next_available(payments, originated.values()),
                    )
                    if moment is not None
                ]
                if not due:
                    break
                clock.set(max(min(due), now))
        return Run(
            scenario=scenario,
            seed=seed,
            manifest=manifest_for(scenario, seed, plans, simulator.faults()),
            ticks=ticks,
            payments=originated,
            summary=ledger_summary(owner),
        )
    finally:
        app_engine.dispose()
        owner.dispose()


def _next_available(payments: Payments, payment_ids: Iterable[int]) -> datetime | None:
    """The earliest time a settled Payment's funds become Available, if any are waiting."""
    due = []
    for payment_id in payment_ids:
        payment = payments.payment(payment_id)
        if payment.state == "settled" and payment.available_at is not None:
            due.append(payment.available_at)
    return min(due) if due else None


def alembic_config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)
    return config


@contextmanager
def fresh_database(server_url: str, prefix: str = "scenario") -> Iterator[str]:
    """A new, migrated database on the Postgres server at `server_url` (whose user may
    create databases), dropped afterwards. Yields its URL."""
    name = f"{prefix}_{uuid4().hex[:10]}"
    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        url = make_url(server_url).set(database=name).render_as_string(hide_password=False)
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
