"""The `marketplace` schema's defences, proved against the database directly (migration
0007): Auction Closes, lots, Payouts and their steps are append-only like the Ledger."""

import psycopg.errors
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration

# A plain column per table (identity columns refuse an UPDATE for their own reason).
COLUMNS = {
    "auction_closes": "currency",
    "lots": "lot_id",
    "payouts": "amount",
    "payout_steps": "detail",
}
EDITS = {
    "update": "UPDATE marketplace.{table} SET {column} = {column}",
    "delete": "DELETE FROM marketplace.{table}",
    "truncate": "TRUNCATE marketplace.{table} CASCADE",
}


@pytest.mark.parametrize("table", COLUMNS)
@pytest.mark.parametrize("edit", EDITS)
def test_the_app_role_may_not_edit_marketplace_history(
    app_engine: Engine, table: str, edit: str
) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        connection.execute(text(EDITS[edit].format(table=table, column=COLUMNS[table])))

    assert type(raised.value.orig) is psycopg.errors.InsufficientPrivilege


@pytest.mark.parametrize("table", COLUMNS)
@pytest.mark.parametrize("edit", EDITS)
def test_the_trigger_stops_even_the_owner_editing_marketplace_history(
    engine: Engine, table: str, edit: str
) -> None:
    with pytest.raises(DBAPIError, match="append-only") as raised, engine.begin() as connection:
        connection.execute(text(EDITS[edit].format(table=table, column=COLUMNS[table])))

    assert type(raised.value.orig) is psycopg.errors.RestrictViolation


@pytest.mark.parametrize("table", COLUMNS)
def test_the_app_role_can_read_and_append_marketplace_rows(app_engine: Engine, table: str) -> None:
    with app_engine.connect() as connection:
        connection.execute(text(f"SELECT count(*) FROM marketplace.{table}"))
        privileges = connection.execute(
            text(
                "SELECT has_table_privilege(:table, 'INSERT'),"
                " has_table_privilege(:table, 'UPDATE')"
            ),
            {"table": f"marketplace.{table}"},
        ).one()

    assert tuple(privileges) == (True, False)


def test_an_rtp_pull_is_refused_by_the_database_too(engine: Engine) -> None:
    with pytest.raises(DBAPIError, match="payments_rtp_is_push_only"), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO ledger.accounts (code, type, currency, recorded_at)"
                " VALUES ('customer:rtp-pull-check:usd', 'liability', 'USD', now())"
                " ON CONFLICT DO NOTHING"
            )
        )
        c.execute(
            text(
                "INSERT INTO payments.payments (idempotency_key, payload_fingerprint, provider,"
                " provider_payment_id, rail, direction, customer_account, currency, amount,"
                " recorded_at) VALUES ('rtp-pull', repeat('0', 64), 'simulator', 'x', 'rtp',"
                " 'pull', 'customer:rtp-pull-check:usd', 'USD', 1, now())"
            )
        )
