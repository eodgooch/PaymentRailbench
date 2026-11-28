"""The `payments` schema's defences, proved against the database directly (migrations 0004,
0008, 0009): Payments, the Inbox, its outcomes and state changes, return exceptions,
Settlement Reports, their lines and Provider Adjustment outcomes are append-only like the
Ledger, the Inbox holds each Provider event ID once, and one report is ingested per Provider
and day."""

import psycopg.errors
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from tests.integration.helpers import unique

pytestmark = pytest.mark.integration

# A plain column per table (identity columns refuse an UPDATE for their own reason).
COLUMNS = {
    "payments": "description",
    "inbox": "provider",
    "inbox_outcomes": "detail",
    "transitions": "state",
    "return_exceptions": "detail",
    "settlement_reports": "detail",
    "settlement_report_lines": "reason",
    "provider_adjustments": "detail",
}
TABLES = tuple(COLUMNS)
EDITS = {
    "update": "UPDATE payments.{table} SET {column} = {column}",
    "delete": "DELETE FROM payments.{table}",
    "truncate": "TRUNCATE payments.{table} CASCADE",
}


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("edit", EDITS)
def test_the_app_role_may_not_edit_payments_history(
    app_engine: Engine, table: str, edit: str
) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        connection.execute(text(EDITS[edit].format(table=table, column=COLUMNS[table])))

    assert type(raised.value.orig) is psycopg.errors.InsufficientPrivilege


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("edit", EDITS)
def test_the_trigger_stops_even_the_owner_editing_payments_history(
    engine: Engine, table: str, edit: str
) -> None:
    with pytest.raises(DBAPIError, match="append-only") as raised, engine.begin() as connection:
        connection.execute(text(EDITS[edit].format(table=table, column=COLUMNS[table])))

    assert type(raised.value.orig) is psycopg.errors.RestrictViolation


# `faults` is the Fault Manifest: the Ledger must never read it (SPEC §6, ADR 0011).
@pytest.mark.parametrize("table", ["payments", "events", "plans", "faults", "settlement_reports"])
def test_the_app_role_cannot_reach_the_simulators_state(app_engine: Engine, table: str) -> None:
    with pytest.raises(DBAPIError) as raised, app_engine.connect() as connection:
        connection.execute(text(f"SELECT count(*) FROM simulator.{table}"))

    assert type(raised.value.orig) is psycopg.errors.InsufficientPrivilege


def test_the_inbox_holds_each_provider_event_once(app_engine: Engine) -> None:
    event_id = unique("evt")
    insert = text(
        "INSERT INTO payments.inbox (provider, event_id, body, headers, received_at)"
        " VALUES ('simulator', :event_id, '\\x7b7d', '{}', now())"
    )
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        connection.execute(insert, {"event_id": event_id})
        connection.execute(insert, {"event_id": event_id})

    assert type(raised.value.orig) is psycopg.errors.UniqueViolation


def test_a_report_is_ingested_once_per_provider_and_day(app_engine: Engine) -> None:
    insert = text(
        "INSERT INTO payments.settlement_reports"
        " (provider, report_date, body, body_sha256, headers, received_at, outcome)"
        " VALUES ('simulator', '1999-01-04', :body, :sha, '{}', now(), 'ingested')"
    )
    with pytest.raises(DBAPIError) as raised, app_engine.begin() as connection:
        connection.execute(insert, {"body": b"a", "sha": "a" * 64})
        connection.execute(insert, {"body": b"b", "sha": "b" * 64})

    assert type(raised.value.orig) is psycopg.errors.UniqueViolation
