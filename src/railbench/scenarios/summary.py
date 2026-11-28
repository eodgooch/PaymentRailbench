"""A canonical picture of the Ledger, for comparing two databases (ADR 0011).

Two Ledgers are the same when they hold the same Transactions, Entries, Settlements, Holds
and Hold ends, and the same Payments with the same state changes and Inbox outcomes, with
the same amounts, Accounts, Effective Times, Recorded Times and Actors. What differs between
databases is only their surrogate keys (sequence-generated IDs), so the summary names every
row by something stable instead:

- Transactions, Settlements, Holds and Hold ends by their Idempotency Key, a Payments step's
  key (`ledger_key`, which holds the Payment's database ID) renamed `<Payment's Idempotency
  Key>/<step>`;
- Payments by Idempotency Key;
- a Provider Event Actor by the Provider event ID instead of its Inbox row ID.

`accounts` narrows the summary to the Payments funding those Customer Balances and the
Ledger rows that touch them or belong to those Payments, for databases shared with other
work (the property tests). Without it, the whole Ledger is summarised.
"""

from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, select

from railbench.ledger.tables import accounts, entries, hold_ends, holds, settlements, transactions
from railbench.payments.service import parse_ledger_key
from railbench.payments.tables import inbox, inbox_outcomes, payments, transitions

Summary = dict[str, Any]


def ledger_summary(engine: Engine, accounts_in_scope: Collection[str] | None = None) -> Summary:
    with engine.connect() as connection:
        return _Summariser(connection, accounts_in_scope).summary()


class _Summariser:
    def __init__(self, connection: Connection, scope: Collection[str] | None) -> None:
        self._c = connection
        self._codes = {row.id: row.code for row in connection.execute(select(accounts))}
        self._events = {
            str(row.id): row.event_id
            for row in connection.execute(select(inbox.c.id, inbox.c.event_id))
        }
        rows = connection.execute(select(payments)).all()
        if scope is not None:
            rows = [row for row in rows if row.customer_account in scope]
        self._payments = {row.id: row for row in rows}
        self._scope = None if scope is None else set(scope)

    def summary(self) -> Summary:
        transaction_rows = {
            row.id: row
            for row in self._c.execute(select(transactions))
            if self._wanted_key(row.idempotency_key)
        }
        entry_rows = self._c.execute(select(entries).order_by(entries.c.id)).all()
        if self._scope is not None:
            touching = {
                e.transaction_id for e in entry_rows if self._codes[e.account_id] in self._scope
            }
            transaction_rows.update(
                {row.id: row for row in self._c.execute(select(transactions)) if row.id in touching}
            )
        found_transactions = {}
        settlement_rows = {row.transaction_id: row for row in self._c.execute(select(settlements))}
        for row in transaction_rows.values():
            found_transactions[self._key(row.idempotency_key)] = {
                "status": row.status,
                "description": row.description,
                "recorded_at": _time(row.recorded_at),
                "reverses": None
                if row.reverses_id is None
                else self._key(self._transaction_key(row.reverses_id)),
                "entries": sorted(
                    (
                        self._codes[e.account_id],
                        e.currency,
                        e.amount,
                        _time(e.effective_at),
                        _time(e.recorded_at),
                        self._actor(e),
                    )
                    for e in entry_rows
                    if e.transaction_id == row.id
                ),
                "settlement": self._settlement(settlement_rows.get(row.id)),
            }
        return {
            "payments": self._summarise_payments(),
            "transactions": dict(sorted(found_transactions.items())),
            "holds": self._holds(),
        }

    def _summarise_payments(self) -> dict[str, Any]:
        steps = self._c.execute(select(transitions).order_by(transitions.c.id)).all()
        outcomes = self._c.execute(select(inbox_outcomes).order_by(inbox_outcomes.c.id)).all()
        found = {}
        for row in self._payments.values():
            found[row.idempotency_key] = {
                "provider": row.provider,
                "provider_payment_id": row.provider_payment_id,
                "customer_account": row.customer_account,
                "currency": row.currency,
                "amount": row.amount,
                "description": row.description,
                "recorded_at": _time(row.recorded_at),
                "transitions": [
                    {
                        "state": s.state,
                        "effective_at": _time(s.effective_at),
                        "recorded_at": _time(s.recorded_at),
                        "actor": self._events[str(s.inbox_id)]
                        if s.inbox_id is not None
                        else f"{s.job_name}@{s.job_version}",
                        "transaction": None
                        if s.transaction_id is None
                        else self._key(self._transaction_key(s.transaction_id)),
                        "hold": None if s.hold_id is None else self._key(self._hold_key(s.hold_id)),
                    }
                    for s in steps
                    if s.payment_id == row.id
                ],
                "inbox_outcomes": [
                    {
                        "event_id": self._events[str(o.inbox_id)],
                        "outcome": o.outcome,
                        "detail": o.detail,
                        "recorded_at": _time(o.recorded_at),
                    }
                    for o in outcomes
                    if o.payment_id == row.id
                ],
            }
        return dict(sorted(found.items()))

    def _holds(self) -> dict[str, Any]:
        ends = {row.hold_id: row for row in self._c.execute(select(hold_ends))}
        found = {}
        for row in self._c.execute(select(holds)):
            code = self._codes[row.account_id]
            if not self._wanted_key(row.idempotency_key) and not (
                self._scope is not None and code in self._scope
            ):
                continue
            end = ends.get(row.id)
            found[self._key(row.idempotency_key)] = {
                "account": code,
                "currency": row.currency,
                "amount": row.amount,
                "description": row.description,
                "effective_at": _time(row.effective_at),
                "recorded_at": _time(row.recorded_at),
                "actor": self._actor(row),
                "end": None
                if end is None
                else {
                    "outcome": end.outcome,
                    "idempotency_key": end.idempotency_key and self._key(end.idempotency_key),
                    "transaction": None
                    if end.transaction_id is None
                    else self._key(self._transaction_key(end.transaction_id)),
                    "effective_at": _time(end.effective_at),
                    "recorded_at": _time(end.recorded_at),
                    "actor": self._actor(end),
                },
            }
        return dict(sorted(found.items()))

    def _settlement(self, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "idempotency_key": self._key(row.idempotency_key),
            "effective_at": _time(row.effective_at),
            "recorded_at": _time(row.recorded_at),
            "actor": self._actor(row),
        }

    def _actor(self, row: Any) -> str:
        match row.actor_kind:
            case "provider_event":
                return f"provider_event:{self._events[row.actor_inbox_id]}"
            case "system_job":
                return f"system_job:{row.actor_job_name}@{row.actor_job_version}"
            case kind:
                return f"{kind}:{row.actor_operator_id}:{row.actor_proposal_id}"

    def _wanted_key(self, key: str) -> bool:
        """Whether a Ledger row with this Idempotency Key is in scope."""
        if self._scope is None:
            return True
        parsed = parse_ledger_key(key)
        return parsed is not None and parsed[0] in self._payments

    def _key(self, key: str) -> str:
        """A Payments step's Idempotency Key named by the Payment's own Idempotency Key and the
        step (`<payment key>/<step>`) instead of the Payment's database id; any other key as
        it is."""
        parsed = parse_ledger_key(key)
        payment = None if parsed is None else self._payments.get(parsed[0])
        if parsed is None or payment is None:
            return key
        return f"{payment.idempotency_key}/{parsed[1]}"

    def _transaction_key(self, transaction_id: int) -> str:
        key: str = self._c.execute(
            select(transactions.c.idempotency_key).where(transactions.c.id == transaction_id)
        ).scalar_one()
        return key

    def _hold_key(self, hold_id: int) -> str:
        key: str = self._c.execute(
            select(holds.c.idempotency_key).where(holds.c.id == hold_id)
        ).scalar_one()
        return key


def _time(moment: datetime) -> str:
    return moment.isoformat()
