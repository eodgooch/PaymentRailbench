"""A canonical picture of the Ledger, for comparing two databases (ADR 0011).

Two Ledgers are the same when they hold the same Transactions, Entries, Settlements, Holds
and Hold ends, and the same Payments with the same state changes, Inbox outcomes and return
exceptions, with
the same amounts, Accounts, Effective Times, Recorded Times and Actors. What differs between
databases is only their surrogate keys (sequence-generated IDs), so the summary names every
row by something stable instead:

- Transactions, Settlements, Holds and Hold ends by their Idempotency Key, a Payments step's
  key (`ledger_key`, which holds the Payment's database ID) renamed `<Payment's Idempotency
  Key>/<step>`;
- Payments by Idempotency Key;
- a Provider Event Actor by the Provider event ID instead of its Inbox row ID;
- Auction Closes by auction ID, their lots by lot ID and their Payouts by seller, each lot's
  Transaction and each Payout step's Hold by Idempotency Key;
- Settlement Reports by `<provider>:<report date>`, every version received in order, with
  its lines as stated and every decision about each Provider Adjustment (pending, booked,
  unmatched) with its Payment (by Idempotency Key) and Transaction (by its key).

`accounts` narrows the summary to the Payments funding those Customer Balances and the
Ledger rows that touch them or belong to those Payments, for databases shared with other
work (the property tests), and to the Auction Closes whose lots touch them, and leaves
Settlement Reports out (a report covers every Payment). Without it, the whole Ledger is
summarised.
"""

from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, select

from railbench.ledger.tables import accounts, entries, hold_ends, holds, settlements, transactions
from railbench.marketplace.tables import auction_closes, lots, payout_steps, payouts
from railbench.payments.accounts import parse_ledger_key
from railbench.payments.reports import LINE_FIELDS
from railbench.payments.tables import (
    inbox,
    inbox_outcomes,
    payments,
    provider_adjustments,
    return_exceptions,
    settlement_report_lines,
    settlement_reports,
    transitions,
)

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
            "auction_closes": self._auction_closes(),
            "settlement_reports": self._settlement_reports(),
        }

    def _settlement_reports(self) -> dict[str, Any]:
        if self._scope is not None:
            return {}
        line_rows = self._c.execute(
            select(settlement_report_lines).order_by(
                settlement_report_lines.c.report_id, settlement_report_lines.c.position
            )
        ).all()
        decisions: dict[int, list[Any]] = {}
        for row in self._c.execute(
            select(provider_adjustments).order_by(provider_adjustments.c.id)
        ):
            decisions.setdefault(row.line_id, []).append(row)
        keys = {row.id: row.idempotency_key for row in self._c.execute(select(payments))}
        found: dict[str, list[Any]] = {}
        reports = self._c.execute(select(settlement_reports).order_by(settlement_reports.c.id))
        for report in reports:
            found.setdefault(f"{report.provider}:{report.report_date.isoformat()}", []).append(
                {
                    "body_sha256": report.body_sha256,
                    "outcome": report.outcome,
                    "detail": report.detail,
                    "received_at": _time(report.received_at),
                    "lines": [
                        {
                            "position": line.position,
                            **{
                                name: _time(value) if isinstance(value, datetime) else value
                                for name in LINE_FIELDS
                                for value in (getattr(line, name),)
                            },
                            "adjustments": [
                                self._adjustment(row, keys) for row in decisions.get(line.id, [])
                            ],
                        }
                        for line in line_rows
                        if line.report_id == report.id
                    ],
                }
            )
        return dict(sorted(found.items()))

    def _adjustment(self, row: Any, keys: dict[int, str]) -> dict[str, Any]:
        return {
            "outcome": row.outcome,
            "payment": None if row.payment_id is None else keys[row.payment_id],
            "transaction": None
            if row.transaction_id is None
            else self._key(self._transaction_key(row.transaction_id)),
            "detail": row.detail,
            "recorded_at": _time(row.recorded_at),
        }

    def _auction_closes(self) -> dict[str, Any]:
        lot_rows = self._c.execute(select(lots).order_by(lots.c.id)).all()
        payout_rows = self._c.execute(select(payouts).order_by(payouts.c.id)).all()
        step_rows = self._c.execute(select(payout_steps).order_by(payout_steps.c.id)).all()
        found = {}
        for close in self._c.execute(select(auction_closes)):
            its_lots = [lot for lot in lot_rows if lot.auction_close_id == close.id]
            touched = {code for lot in its_lots for code in (lot.buyer, lot.seller)}
            if self._scope is not None and not touched & self._scope:
                continue
            found[close.auction_id] = {
                "currency": close.currency,
                "closed_at": _time(close.closed_at),
                "seller_fee_bps": close.seller_fee_bps,
                "instant_payout_limit": close.instant_payout_limit,
                "recorded_at": _time(close.recorded_at),
                "lots": {
                    lot.lot_id: {
                        "buyer": lot.buyer,
                        "seller": lot.seller,
                        "hammer_price": lot.hammer_price,
                        "fee": lot.fee,
                        "transaction": self._transaction_key(lot.transaction_id),
                    }
                    for lot in its_lots
                },
                "payouts": {
                    payout.customer_account: {
                        "amount": payout.amount,
                        "currency": payout.currency,
                        "requested": payout.requested,
                        "recorded_at": _time(payout.recorded_at),
                        "steps": [
                            {
                                "step": step.step,
                                "detail": step.detail,
                                "hold": None
                                if step.hold_id is None
                                else self._hold_key(step.hold_id),
                                "job": f"{step.job_name}@{step.job_version}",
                                "effective_at": _time(step.effective_at),
                                "recorded_at": _time(step.recorded_at),
                            }
                            for step in step_rows
                            if step.payout_id == payout.id
                        ],
                    }
                    for payout in payout_rows
                    if payout.auction_close_id == close.id
                },
            }
        return dict(sorted(found.items()))

    def _summarise_payments(self) -> dict[str, Any]:
        steps = self._c.execute(select(transitions).order_by(transitions.c.id)).all()
        outcomes = self._c.execute(select(inbox_outcomes).order_by(inbox_outcomes.c.id)).all()
        raised = self._c.execute(select(return_exceptions).order_by(return_exceptions.c.id)).all()
        found = {}
        for row in self._payments.values():
            found[row.idempotency_key] = {
                "provider": row.provider,
                "provider_payment_id": row.provider_payment_id,
                "rail": row.rail,
                "direction": row.direction,
                "customer_account": row.customer_account,
                "currency": row.currency,
                "amount": row.amount,
                "description": row.description,
                "recorded_at": _time(row.recorded_at),
                "hold": None if row.hold_id is None else self._key(self._hold_key(row.hold_id)),
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
                "return_exceptions": [
                    {
                        "event_id": self._events[str(e.inbox_id)],
                        "customer_account": e.customer_account,
                        "recovery_account": e.recovery_account,
                        "currency": e.currency,
                        "return_code": e.return_code,
                        "exposure": e.exposure,
                        "late": e.late,
                        "recovery_transaction": self._key(
                            self._transaction_key(e.recovery_transaction_id)
                        ),
                        "detail": e.detail,
                        "effective_at": _time(e.effective_at),
                        "recorded_at": _time(e.recorded_at),
                    }
                    for e in raised
                    if e.payment_id == row.id
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
