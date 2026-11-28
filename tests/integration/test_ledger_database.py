"""The Ledger's own defences, proved against the database directly (SPEC §3.2, §3.3, ADR 0006,
ADR 0009).

These bypass the Journal on purpose: the grants and triggers have to hold even for code that
gets the Python checks wrong, and even for the database owner.
"""

from datetime import UTC, datetime

import psycopg.errors
import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from railbench.common.clock import ManualClock
from railbench.ledger.journal import Journal
from railbench.ledger.model import (
    AccountType,
    NewAccount,
    NewEntry,
    NewHold,
    NewTransaction,
    ReleaseRequest,
    SettlementRequest,
    SystemJob,
    TransactionStatus,
)
from tests.integration.helpers import unique

pytestmark = pytest.mark.integration

AUCTION_CLOSE = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)
TABLES = ("accounts", "transactions", "entries", "settlements", "holds", "hold_ends")
EDITS = {
    "update": "UPDATE ledger.{table} SET recorded_at = recorded_at",
    "delete": "DELETE FROM ledger.{table}",
    "truncate": "TRUNCATE ledger.{table} CASCADE",
}


ACH_JOB = SystemJob(name="ach-settlement", version="1.0.0")


@pytest.fixture
def posted(app_engine: Engine) -> int:
    """A posted Transaction, plus a settled pending one and a released Hold, so every Ledger
    table has a row to try to edit."""
    journal = Journal(app_engine, ManualClock(AUCTION_CLOSE))
    codes = [unique("clearing"), unique("customer")]
    for code, type_ in zip(codes, (AccountType.ASSET, AccountType.LIABILITY), strict=True):
        journal.open_account(NewAccount(code=code, type=type_, currency="USD"))
    funding = NewTransaction(
        idempotency_key=unique("funding"),
        actor=ACH_JOB,
        effective_at=AUCTION_CLOSE,
        entries=[
            NewEntry(account=codes[0], currency="USD", amount=500),
            NewEntry(account=codes[1], currency="USD", amount=-500),
        ],
    )
    pending = journal.post(
        funding.model_copy(
            update={"idempotency_key": unique("pending"), "status": TransactionStatus.PENDING}
        )
    ).transaction
    journal.settle(
        pending.id,
        SettlementRequest(
            idempotency_key=unique("settlement"), actor=ACH_JOB, effective_at=AUCTION_CLOSE
        ),
    )
    hold = journal.place_hold(
        NewHold(
            idempotency_key=unique("hold"),
            actor=ACH_JOB,
            effective_at=AUCTION_CLOSE,
            account=codes[1],
            currency="USD",
            amount=100,
        )
    ).hold
    journal.release_hold(
        hold.id,
        ReleaseRequest(
            idempotency_key=unique("release"), actor=ACH_JOB, effective_at=AUCTION_CLOSE
        ),
    )
    return journal.post(funding).transaction.id


def sqlstate(error: DBAPIError) -> type[BaseException | None]:
    """The psycopg exception class, which names the SQLSTATE."""
    return type(error.orig)


@pytest.mark.usefixtures("posted")
@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("edit", EDITS)
def test_the_app_role_may_not_edit_history(app_engine: Engine, table: str, edit: str) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        connection.execute(text(EDITS[edit].format(table=table)))

    assert sqlstate(raised.value) is psycopg.errors.InsufficientPrivilege


@pytest.mark.usefixtures("posted")
def test_the_app_role_may_not_edit_history_even_with_the_migration_setting(
    app_engine: Engine,
) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        connection.execute(text("SET LOCAL railbench.migrating = 'on'"))
        connection.execute(text("DELETE FROM ledger.entries"))

    assert sqlstate(raised.value) is psycopg.errors.InsufficientPrivilege


@pytest.mark.usefixtures("posted")
@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("edit", EDITS)
def test_the_trigger_stops_even_the_owner_editing_history(
    engine: Engine, table: str, edit: str
) -> None:
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))

    with pytest.raises(DBAPIError, match="append-only") as raised, engine.begin() as connection:
        connection.execute(text(EDITS[edit].format(table=table)))

    assert sqlstate(raised.value) is psycopg.errors.RestrictViolation


def test_migrations_can_still_change_ledger_rows(engine: Engine, posted: int) -> None:
    with engine.connect() as connection, connection.begin() as transaction:
        connection.execute(text("SET LOCAL railbench.migrating = 'on'"))
        changed = connection.execute(
            text("UPDATE ledger.transactions SET description = 'backfilled' WHERE id = :id"),
            {"id": posted},
        )
        assert changed.rowcount == 1
        transaction.rollback()


def insert_transaction(connection: Connection, status: str = "posted") -> int:
    transaction_id: int = connection.execute(
        text(
            "INSERT INTO ledger.transactions"
            " (idempotency_key, payload_fingerprint, status, recorded_at)"
            " VALUES (:key, repeat('0', 64), :status, now()) RETURNING id"
        ),
        {"key": unique("raw"), "status": status},
    ).scalar_one()
    return transaction_id


def account_id(connection: Connection, currency: str = "USD") -> int:
    code = unique("raw-account")
    connection.execute(
        text(
            "INSERT INTO ledger.accounts (code, type, currency, recorded_at)"
            " VALUES (:code, 'asset', :currency, now())"
        ),
        {"code": code, "currency": currency},
    )
    found: int = connection.execute(
        text("SELECT id FROM ledger.accounts WHERE code = :code"), {"code": code}
    ).scalar_one()
    return found


def insert_entry(
    connection: Connection,
    transaction_id: int,
    account: int,
    amount: int,
    *,
    currency: str = "USD",
    actor: str = "actor_kind, actor_job_name, actor_job_version",
    actor_values: str = "'system_job', 'raw-sql', '0'",
) -> None:
    connection.execute(
        text(
            "INSERT INTO ledger.entries (transaction_id, account_id, currency, amount,"
            f" effective_at, recorded_at, {actor})"
            f" VALUES (:txn, :account, :currency, :amount, now(), now(), {actor_values})"
        ),
        {"txn": transaction_id, "account": account, "currency": currency, "amount": amount},
    )


def test_an_unbalanced_transaction_fails_at_commit(app_engine: Engine) -> None:
    with (
        pytest.raises(DBAPIError, match="does not balance") as raised,
        app_engine.begin() as connection,
    ):
        transaction_id = insert_transaction(connection)
        insert_entry(connection, transaction_id, account_id(connection), 100)
        insert_entry(connection, transaction_id, account_id(connection), -99)

    assert sqlstate(raised.value) is psycopg.errors.CheckViolation


def test_a_transaction_balances_per_currency_at_commit(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError, match="does not balance"), app_engine.begin() as connection:
        transaction_id = insert_transaction(connection)
        insert_entry(connection, transaction_id, account_id(connection, "USD"), 100)
        insert_entry(
            connection, transaction_id, account_id(connection, "USDC"), -100, currency="USDC"
        )


def test_a_balanced_transaction_can_be_built_up_entry_by_entry(app_engine: Engine) -> None:
    # The check is deferred: it only runs at commit, once every Entry is in.
    with app_engine.begin() as connection:
        transaction_id = insert_transaction(connection)
        insert_entry(connection, transaction_id, account_id(connection), 100)
        insert_entry(connection, transaction_id, account_id(connection), -60)
        insert_entry(connection, transaction_id, account_id(connection), -40)


def test_a_transaction_without_entries_fails_at_commit(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError, match="has no Entries"), app_engine.begin() as connection:
        insert_transaction(connection)


def test_an_entry_cannot_be_zero(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        insert_entry(connection, insert_transaction(connection), account_id(connection), 0)

    assert sqlstate(raised.value) is psycopg.errors.CheckViolation


def test_an_entry_must_be_in_its_accounts_currency(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        transaction_id = insert_transaction(connection)
        insert_entry(connection, transaction_id, account_id(connection, "USD"), 1, currency="EUR")

    assert sqlstate(raised.value) is psycopg.errors.ForeignKeyViolation


@pytest.mark.parametrize(
    ("columns", "values"),
    [
        pytest.param("actor_kind", "'system_job'", id="system job without name or version"),
        pytest.param(
            "actor_kind, actor_job_name, actor_job_version",
            "'system_job', '', '1'",
            id="system job with an empty name",
        ),
        pytest.param("actor_kind", "'provider_event'", id="provider event without inbox id"),
        pytest.param(
            "actor_kind, actor_operator_id", "'person', 'op-1'", id="person without proposal"
        ),
        pytest.param(
            "actor_kind, actor_inbox_id, actor_operator_id, actor_proposal_id",
            "'person', 'in-1', 'op-1', 'prop-1'",
            id="person with a stray inbox id",
        ),
        pytest.param("actor_kind, actor_inbox_id", "'robot', 'in-1'", id="unknown actor kind"),
        pytest.param("actor_job_name", "'raw-sql'", id="no actor kind"),
    ],
)
def test_every_entry_has_a_well_formed_actor(app_engine: Engine, columns: str, values: str) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        insert_entry(
            connection,
            insert_transaction(connection),
            account_id(connection),
            1,
            actor=columns,
            actor_values=values,
        )

    assert sqlstate(raised.value) in {
        psycopg.errors.CheckViolation,
        psycopg.errors.NotNullViolation,
    }


def test_a_transaction_is_reversed_at_most_once(app_engine: Engine, posted: int) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        for _ in range(2):
            connection.execute(
                text(
                    "INSERT INTO ledger.transactions"
                    " (idempotency_key, payload_fingerprint, reverses_id, status, recorded_at)"
                    " VALUES (:key, repeat('0', 64), :original, 'posted', now())"
                ),
                {"key": unique("raw-reversal"), "original": posted},
            )

    assert sqlstate(raised.value) is psycopg.errors.UniqueViolation


SYSTEM_ACTOR = ("actor_kind, actor_job_name, actor_job_version", "'system_job', 'raw-sql', '0'")


def insert_settlement(connection: Connection, transaction_id: int) -> None:
    connection.execute(
        text(
            "INSERT INTO ledger.settlements (transaction_id, idempotency_key,"
            f" payload_fingerprint, effective_at, recorded_at, {SYSTEM_ACTOR[0]})"
            f" VALUES (:txn, :key, repeat('0', 64), now(), now(), {SYSTEM_ACTOR[1]})"
        ),
        {"txn": transaction_id, "key": unique("raw-settlement")},
    )


def pending_transaction(connection: Connection) -> int:
    transaction_id = insert_transaction(connection, status="pending")
    insert_entry(connection, transaction_id, account_id(connection), 100)
    insert_entry(connection, transaction_id, account_id(connection), -100)
    return transaction_id


def test_only_a_pending_transaction_can_settle(app_engine: Engine, posted: int) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        insert_settlement(connection, posted)

    assert sqlstate(raised.value) is psycopg.errors.ForeignKeyViolation


def test_a_transaction_settles_at_most_once(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        transaction_id = pending_transaction(connection)
        insert_settlement(connection, transaction_id)
        insert_settlement(connection, transaction_id)

    assert sqlstate(raised.value) is psycopg.errors.UniqueViolation


def insert_hold(connection: Connection, amount: int = 100) -> int:
    hold_id: int = connection.execute(
        text(
            "INSERT INTO ledger.holds (idempotency_key, payload_fingerprint, account_id,"
            f" currency, amount, effective_at, recorded_at, {SYSTEM_ACTOR[0]})"
            " VALUES (:key, repeat('0', 64), :account, 'USD', :amount, now(), now(),"
            f" {SYSTEM_ACTOR[1]}) RETURNING id"
        ),
        {"key": unique("raw-hold"), "account": account_id(connection), "amount": amount},
    ).scalar_one()
    return hold_id


def end_hold(
    connection: Connection, hold_id: int, outcome: str, transaction_id: int | None = None
) -> None:
    key = None if outcome == "captured" else unique("raw-release")
    connection.execute(
        text(
            "INSERT INTO ledger.hold_ends (hold_id, outcome, transaction_id, idempotency_key,"
            f" payload_fingerprint, effective_at, recorded_at, {SYSTEM_ACTOR[0]})"
            f" VALUES (:hold, :outcome, :txn, :key, :fingerprint, now(), now(), {SYSTEM_ACTOR[1]})"
        ),
        {
            "hold": hold_id,
            "outcome": outcome,
            "txn": transaction_id,
            "key": key,
            "fingerprint": key and "0" * 64,
        },
    )


@pytest.mark.parametrize("amount", [0, -100])
def test_a_hold_amount_is_positive(app_engine: Engine, amount: int) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        insert_hold(connection, amount)

    assert sqlstate(raised.value) is psycopg.errors.CheckViolation


@pytest.mark.parametrize(
    ("first", "second"),
    [("released", "released"), ("captured", "released"), ("released", "captured")],
)
def test_a_hold_ends_at_most_once(app_engine: Engine, posted: int, first: str, second: str) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        hold_id = insert_hold(connection)
        for outcome in (first, second):
            end_hold(connection, hold_id, outcome, posted if outcome == "captured" else None)

    assert sqlstate(raised.value) is psycopg.errors.UniqueViolation


@pytest.mark.parametrize(
    ("outcome", "with_transaction"),
    [
        pytest.param("captured", False, id="a capture without its Transaction"),
        pytest.param("released", True, id="a release with a Transaction"),
    ],
)
def test_a_hold_end_has_the_shape_of_its_outcome(
    app_engine: Engine, posted: int, outcome: str, with_transaction: bool
) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        end_hold(connection, insert_hold(connection), outcome, posted if with_transaction else None)

    assert sqlstate(raised.value) is psycopg.errors.CheckViolation
