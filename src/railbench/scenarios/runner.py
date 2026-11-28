"""Run one Scenario against the Ledger and the Simulator (SPEC §6, ADR 0011).

Both services run in-process on one ManualClock and talk over real localhost HTTP, exactly
as in the end-to-end tests: the Ledger creates Payments at the Simulator through the
Provider interface and receives signed webhooks, Faults included. The runner plays the part
of Mintwharf's buyers and of time:

1. open each Customer's Customer Balance at the start;
2. hand the Simulator its plan for every Payment (outcome and Faults, resolved from the
   seed), the way a test would configure a sandbox;
3. originate each Payment, then close each auction, through the Ledger API at its time;
4. move the Clock straight to the next moment anything is due (a Payment to originate, an
   auction to close, a Payment to settle or end, a webhook to deliver, ACH funds due to
   become Available, a Settlement Report with anything on it to publish), tick the
   Simulator (then have it publish due Settlement Reports) and then the Ledger's jobs (ACH
   availability, Payout release, then Settlement Report ingest), and repeat until nothing
   is left to do;
5. write the Fault Manifest, report Faults included.

No randomness is drawn here and no wall clock is read, so a Scenario and seed give the same
Ledger, Payment and event IDs, delivery order and Fault Manifest every run. The one
dependency on wall time left is the webhook timeout (DELIVERY_TIMEOUT): a delivery that
fails or times out fails the run (DeliveryFailed) rather than being retried off the seed's
course. The database must be migrated and hold no other Scenario's Accounts
(`fresh_database()` makes one).
"""

import secrets
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
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
from railbench.marketplace.service import Marketplace
from railbench.payments.reports import SettlementReports
from railbench.payments.service import Payments
from railbench.scenarios.manifest import Manifest, manifest_for
from railbench.scenarios.model import Scenario, ScenarioAuctionClose
from railbench.scenarios.servers import free_port, serving
from railbench.scenarios.summary import Summary, ledger_summary
from railbench.simulator.app import create_app as create_simulator_app
from railbench.simulator.provider import PROVIDER, Simulator

ALEMBIC_INI = Path(__file__).parents[3] / "alembic.ini"
# A run that hasn't gone quiet after this many ticks is stuck (a bug, not a long Scenario).
MAX_TICKS = 10_000
DELIVERY_TIMEOUT = 60.0  # seconds of wall time: generous, since a timeout fails the run


class RunDidNotSettle(RuntimeError):
    pass


@dataclass(frozen=True)
class Run:
    scenario: Scenario
    seed: int
    manifest: Manifest
    manifest_path: Path
    # Every moment the runner ticked both services, in order (part of what must repeat).
    ticks: list[datetime]
    # Scenario Payment id → the Ledger's Payment id.
    payments: dict[str, int]
    summary: Summary = field(repr=False)
    # Every Settlement Report the Simulator published, by Banking Day, as published.
    settlement_reports: dict[date, bytes] = field(default_factory=dict, repr=False)


def run(
    scenario: Scenario,
    database_url: str,
    seed: int | None = None,
    manifest_path: Path | None = None,
) -> Run:
    """Run `scenario` with `seed` (default: the Scenario's own) against the migrated, empty
    database at `database_url`, and write its Fault Manifest to `manifest_path` (default
    reports/manifests/<scenario>-<seed>.json)."""
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
        # Both ends are this process, so each run signs with a throwaway secret of its own.
        webhook_signing_secret=secrets.token_hex(16),
    ).model_copy(update=_ledger_settings(scenario))
    ledger_app = create_ledger_app(app_engine, clock, settings)
    delivery_http = httpx.Client(timeout=DELIVERY_TIMEOUT)
    simulator_app = create_simulator_app(
        owner,
        clock,
        settings,
        # Every webhook must land first time: a retry would move the run off its seed, so
        # a failed or timed-out delivery fails the run instead of quietly diverging.
        Simulator(
            owner,
            clock,
            webhook_url=settings.ledger_webhook_url,
            signing_secret=settings.webhook_signing_secret.get_secret_value(),
            http=delivery_http,
            fail_on_delivery_error=True,
        ),
    )
    journal: Journal = ledger_app.state.journal
    payments: Payments = ledger_app.state.payments
    marketplace: Marketplace = ledger_app.state.marketplace
    reports: SettlementReports = ledger_app.state.reports
    simulator: Simulator = simulator_app.state.simulator
    try:
        with (
            serving(simulator_app, simulator_port),
            serving(ledger_app, ledger_port),
            httpx.Client(base_url=ledger_url, timeout=10.0) as ledger,
        ):
            for customer in scenario.customers:
                for currency in _currencies(scenario, customer.id):
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
            closing = sorted(scenario.auction_closes, key=lambda a: a.at)
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
                while closing and scenario.start + closing[0].at <= now:
                    close = closing.pop(0)
                    response = ledger.post(
                        "/auction-closes", json=_auction_close(scenario, close, seed, now)
                    )
                    response.raise_for_status()
                simulator.tick()
                simulator.publish_reports()
                payments.make_available()
                marketplace.release_payouts()
                reports.ingest_due()
                ticks.append(now)
                if len(ticks) > MAX_TICKS:
                    raise RunDidNotSettle(f"{scenario.name} still busy after {MAX_TICKS} ticks")
                due = [
                    moment
                    for moment in (
                        simulator.next_due(),
                        simulator.next_report_due(),
                        waiting[0].at + scenario.start if waiting else None,
                        closing[0].at + scenario.start if closing else None,
                        _next_available(payments, originated.values()),
                    )
                    if moment is not None
                ]
                if not due:
                    break
                clock.set(max(min(due), now))
        manifest = manifest_for(scenario, seed, plans, simulator.faults())
        return Run(
            scenario=scenario,
            seed=seed,
            manifest=manifest,
            manifest_path=manifest.write(manifest_path),
            ticks=ticks,
            payments=originated,
            summary=ledger_summary(owner),
            settlement_reports=simulator.published_reports(PROVIDER),
        )
    finally:
        delivery_http.close()
        app_engine.dispose()
        owner.dispose()


def _ledger_settings(scenario: Scenario) -> dict[str, int]:
    settings = {}
    if scenario.instant_payout_limit is not None:
        settings["instant_payout_limit_cents"] = scenario.instant_payout_limit
    if scenario.seller_fee_bps is not None:
        settings["seller_fee_bps"] = scenario.seller_fee_bps
    return settings


def _currencies(scenario: Scenario, customer_id: str) -> list[str]:
    """The currencies a Customer pays or is paid in, one Customer Balance for each."""
    found = {p.currency for p in scenario.payments if p.customer == customer_id}
    for close in scenario.auction_closes:
        if any(customer_id in (lot.buyer, lot.seller) for lot in close.lots):
            found.add(close.currency)
    return sorted(found)


def _auction_close(
    scenario: Scenario, close: ScenarioAuctionClose, seed: int, now: datetime
) -> dict[str, Any]:
    def account(customer_id: str) -> str:
        return scenario.customer(customer_id).account(close.currency)

    return {
        "auction_id": scenario.auction_id(close, seed),
        "closed_at": now.isoformat(),
        "currency": close.currency,
        "lots": [
            {
                "lot_id": lot.id,
                "buyer": account(lot.buyer),
                "seller": account(lot.seller),
                "hammer_price": lot.hammer_price,
            }
            for lot in close.lots
        ],
        "instant_payouts": scenario.instant_payouts(close),
    }


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
