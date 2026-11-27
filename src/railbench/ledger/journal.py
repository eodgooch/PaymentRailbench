"""The Journal: the one way into the Ledger.

`Journal` posts Transactions and Reversals and reads Accounts, Transactions and Posted
Balances. It validates in code before writing, and the database checks the same rules again
(migration 0002). It needs only SELECT and INSERT, so it runs as the append-only app role.

Idempotency rests on the unique Idempotency Key, not on check-then-insert: the Transaction row
goes in with `ON CONFLICT DO NOTHING`, so of two concurrent posts with one key, Postgres lets
exactly one insert and the other sees the committed original. Reversals rely the same way on
`reverses_id` being unique.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, Row, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from railbench.common.clock import Clock
from railbench.ledger.model import (
    Account,
    AccountType,
    Actor,
    Entry,
    NewAccount,
    NewEntry,
    NewTransaction,
    Person,
    Posted,
    ProviderEventActor,
    ReversalRequest,
    SystemJob,
    Transaction,
)
from railbench.ledger.tables import accounts, entries, transactions


class LedgerError(Exception):
    """A request the Ledger refuses. Subclasses say why."""


class NotFound(LedgerError):
    pass


class Conflict(LedgerError):
    pass


class Rejected(LedgerError):
    """The request is well formed but the Ledger can't post it."""


class AccountNotFound(NotFound):
    pass


class TransactionNotFound(NotFound):
    pass


class DuplicateAccount(Conflict):
    pass


class IdempotencyConflict(Conflict):
    """The Idempotency Key was already posted with a different payload."""


class AlreadyReversed(Conflict):
    pass


class CannotReverseReversal(Conflict):
    """A Reversal is final; to undo one, post a new Transaction."""


class UnknownAccount(Rejected):
    pass


class CurrencyMismatch(Rejected):
    pass


class Journal:
    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    def open_account(self, new: NewAccount) -> Account:
        values = {**new.model_dump(), "recorded_at": self._clock.now()}
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(accounts).values(values))
        except IntegrityError as error:
            if _constraint(error) == "accounts_code_key":
                raise DuplicateAccount(f"Account {new.code} already exists") from error
            raise
        return Account.model_validate(values)

    def account(self, code: str) -> Account:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    accounts.c.code, accounts.c.type, accounts.c.currency, accounts.c.recorded_at
                ).where(accounts.c.code == code)
            ).one_or_none()
        if row is None:
            raise AccountNotFound(f"no Account {code}")
        return Account(
            code=row.code,
            type=AccountType(row.type),
            currency=row.currency,
            recorded_at=row.recorded_at,
        )

    def posted_balance(self, code: str) -> int:
        """The sum of the Account's Entries, in its currency's Minor Units. Derived on every
        read, never stored (SPEC §3.3)."""
        with self._engine.connect() as connection:
            account_id = connection.scalar(select(accounts.c.id).where(accounts.c.code == code))
            if account_id is None:
                raise AccountNotFound(f"no Account {code}")
            total = connection.execute(
                select(func.coalesce(func.sum(entries.c.amount), 0)).where(
                    entries.c.account_id == account_id
                )
            ).scalar_one()
        return int(total)

    def post(self, new: NewTransaction) -> Posted:
        """Post a Transaction exactly once per Idempotency Key."""
        return self._write(
            key=new.idempotency_key,
            fingerprint=_fingerprint({"post": new.model_dump(exclude={"idempotency_key"})}),
            description=new.description,
            reverses=None,
            actor=new.actor,
            effective_at=new.effective_at,
            lines=new.entries,
        )

    def reverse(self, transaction_id: int, request: ReversalRequest) -> Posted:
        """Post a Reversal: a Transaction that negates every Entry of the original and
        references it. A Transaction can be reversed once, and a Reversal can't be reversed."""
        original = self.transaction(transaction_id)
        if original.reverses is not None:
            raise CannotReverseReversal(
                f"Transaction {transaction_id} is a Reversal; post a new Transaction instead"
            )
        payload = {"reverse": transaction_id, **request.model_dump(exclude={"idempotency_key"})}
        try:
            return self._write(
                key=request.idempotency_key,
                fingerprint=_fingerprint(payload),
                description=request.description,
                reverses=transaction_id,
                actor=request.actor,
                effective_at=request.effective_at,
                lines=[
                    NewEntry(account=entry.account, currency=entry.currency, amount=-entry.amount)
                    for entry in original.entries
                ],
            )
        except IntegrityError as error:
            if _constraint(error) == "transactions_reverses_id_key":
                raise AlreadyReversed(
                    f"Transaction {transaction_id} has already been reversed"
                ) from error
            raise

    def transaction(self, transaction_id: int) -> Transaction:
        with self._engine.connect() as connection:
            return _read_transaction(connection, transaction_id)

    def _write(
        self,
        *,
        key: str,
        fingerprint: str,
        description: str,
        reverses: int | None,
        actor: Actor,
        effective_at: datetime,
        lines: Sequence[NewEntry],
    ) -> Posted:
        recorded_at = self._clock.now()
        with self._engine.begin() as connection:
            transaction_id: int | None = connection.scalar(
                pg_insert(transactions)
                .values(
                    idempotency_key=key,
                    payload_fingerprint=fingerprint,
                    description=description,
                    reverses_id=reverses,
                    recorded_at=recorded_at,
                )
                .on_conflict_do_nothing(index_elements=[transactions.c.idempotency_key])
                .returning(transactions.c.id)
            )
            if transaction_id is not None:
                account_ids = _resolve_accounts(connection, lines)
                connection.execute(
                    insert(entries),
                    [
                        {
                            "transaction_id": transaction_id,
                            "account_id": account_ids[line.account],
                            "currency": line.currency,
                            "amount": line.amount,
                            "effective_at": effective_at,
                            "recorded_at": recorded_at,
                            **_actor_columns(actor),
                        }
                        for line in lines
                    ],
                )
        # Read after commit, once the deferred balance check has passed.
        with self._engine.connect() as connection:
            if transaction_id is not None:
                return Posted(
                    transaction=_read_transaction(connection, transaction_id), replayed=False
                )
            original = connection.execute(
                select(transactions.c.id, transactions.c.payload_fingerprint).where(
                    transactions.c.idempotency_key == key
                )
            ).one()
            if original.payload_fingerprint != fingerprint:
                raise IdempotencyConflict(
                    f"Idempotency Key {key!r} was already posted with a different payload"
                )
            return Posted(transaction=_read_transaction(connection, original.id), replayed=True)


def _resolve_accounts(connection: Connection, lines: Sequence[NewEntry]) -> dict[str, int]:
    codes = {line.account for line in lines}
    found = {
        row.code: row
        for row in connection.execute(
            select(accounts.c.id, accounts.c.code, accounts.c.currency).where(
                accounts.c.code.in_(codes)
            )
        )
    }
    missing = sorted(codes - found.keys())
    if missing:
        raise UnknownAccount(f"no such Account: {', '.join(missing)}")
    for line in lines:
        currency = found[line.account].currency
        if currency != line.currency:
            raise CurrencyMismatch(f"Account {line.account} is in {currency}, not {line.currency}")
    return {code: row.id for code, row in found.items()}


def _read_transaction(connection: Connection, transaction_id: int) -> Transaction:
    row = connection.execute(
        select(transactions).where(transactions.c.id == transaction_id)
    ).one_or_none()
    if row is None:
        raise TransactionNotFound(f"no Transaction {transaction_id}")
    reversed_by = connection.scalar(
        select(transactions.c.id).where(transactions.c.reverses_id == transaction_id)
    )
    entry_rows = connection.execute(
        select(entries, accounts.c.code.label("account_code"))
        .join(accounts, accounts.c.id == entries.c.account_id)
        .where(entries.c.transaction_id == transaction_id)
        .order_by(entries.c.id)
    )
    return Transaction(
        id=row.id,
        idempotency_key=row.idempotency_key,
        description=row.description,
        recorded_at=row.recorded_at,
        reverses=row.reverses_id,
        reversed_by=reversed_by,
        entries=[
            Entry(
                id=entry.id,
                account=entry.account_code,
                currency=entry.currency,
                amount=entry.amount,
                effective_at=entry.effective_at,
                recorded_at=entry.recorded_at,
                actor=_actor_from(entry),
            )
            for entry in entry_rows
        ],
    )


def _actor_columns(actor: Actor) -> dict[str, str | None]:
    columns: dict[str, str | None] = {
        "actor_kind": actor.kind,
        "actor_job_name": None,
        "actor_job_version": None,
        "actor_inbox_id": None,
        "actor_operator_id": None,
        "actor_proposal_id": None,
    }
    match actor:
        case SystemJob(name=name, version=version):
            columns |= {"actor_job_name": name, "actor_job_version": version}
        case ProviderEventActor(inbox_id=inbox_id):
            columns |= {"actor_inbox_id": inbox_id}
        case Person(operator_id=operator_id, proposal_id=proposal_id):
            columns |= {"actor_operator_id": operator_id, "actor_proposal_id": proposal_id}
    return columns


def _actor_from(row: Row[Any]) -> Actor:
    match row.actor_kind:
        case "system_job":
            return SystemJob(name=row.actor_job_name, version=row.actor_job_version)
        case "provider_event":
            return ProviderEventActor(inbox_id=row.actor_inbox_id)
        case "person":
            return Person(operator_id=row.actor_operator_id, proposal_id=row.actor_proposal_id)
    raise ValueError(f"unknown Actor kind {row.actor_kind!r}")


def _fingerprint(payload: dict[str, Any]) -> str:
    """A SHA-256 of the request's canonical JSON, to tell a retry from a different request
    reusing its Idempotency Key."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_instant)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _instant(value: object) -> str:
    # Times compare as instants: 16:00-05:00 and 21:00Z are the same payload.
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"can't fingerprint {type(value).__name__}")


def _constraint(error: IntegrityError) -> str | None:
    diag = getattr(error.orig, "diag", None)
    name: str | None = getattr(diag, "constraint_name", None)
    return name
