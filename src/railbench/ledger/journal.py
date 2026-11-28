"""The Journal: the one way into the Ledger.

`Journal` posts Transactions, Reversals and Settlements, places, releases and captures Holds,
and reads Accounts, Transactions, Holds and bitemporal Balances. It validates in code before
writing, and the database checks the same rules again (migrations 0002 and 0003). It needs
only SELECT and INSERT, so it runs as the append-only app role.

Idempotency rests on unique Idempotency Keys, not on check-then-insert: a row goes in with
`ON CONFLICT DO NOTHING` (or its unique violation is caught), so of two concurrent requests
with one key, Postgres lets exactly one insert and the other sees the committed original.
"At most once" rules rely the same way on unique columns: `reverses_id` for Reversals,
`settlements.transaction_id` for Settlements, `hold_ends.hold_id` for releases and captures.

Balances are derived on every read, never stored or cached (ADR 0009).
"""

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    Row,
    Table,
    case,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from railbench.common.clock import Clock
from railbench.ledger.model import (
    Account,
    AccountType,
    Actor,
    Balance,
    Entry,
    Hold,
    HoldEnd,
    HoldOutcome,
    HoldRecorded,
    NewAccount,
    NewEntry,
    NewHold,
    NewTransaction,
    Person,
    Posted,
    ProviderEventActor,
    ReleaseRequest,
    ReversalRequest,
    Settlement,
    SettlementRequest,
    SystemJob,
    Transaction,
    TransactionStatus,
)
from railbench.ledger.tables import accounts, entries, hold_ends, holds, settlements, transactions


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


class HoldNotFound(NotFound):
    pass


class AlreadySettled(Conflict):
    pass


class HoldAlreadyEnded(Conflict):
    """A Hold is released or captured once, and never both."""


class NotSettleable(Rejected):
    """Only a pending Transaction settles; a Reversal settles with the Transaction it
    reverses."""


class TooEarly(Rejected):
    """A Settlement or the end of a Hold can't take effect before what it ends."""


class CaptureMissesHold(Rejected):
    """A capture must move money on the held Account."""


class NotYetKnown(Rejected):
    """A balance can't be asked for as known at a time the Ledger hasn't reached."""


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

    def balance(
        self, code: str, effective_at: datetime, known_at: datetime | None = None
    ) -> Balance:
        """The Account's Posted, Pending and Available Balances at `effective_at`, as known at
        `known_at` (default: now), in the Account's normal direction (ADR 0009).

        Both bounds are inclusive: an Entry, Settlement, Hold or end of a Hold counts when
        its Effective Time is at or before `effective_at` and its Recorded Time is at or
        before `known_at`. Asking as known at a time after now is refused, since that answer
        could still change.
        """
        now = self._clock.now()
        known_at = now if known_at is None else known_at
        for moment in (effective_at, known_at):
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError("balance times must be timezone-aware")
        if known_at > now:
            raise NotYetKnown(f"the Ledger can't know {known_at.isoformat()} yet")
        with self._engine.connect() as connection:
            account = connection.execute(
                select(accounts.c.id, accounts.c.type, accounts.c.currency).where(
                    accounts.c.code == code
                )
            ).one_or_none()
            if account is None:
                raise AccountNotFound(f"no Account {code}")
            sign = AccountType(account.type).normal_sign
            posted, pending, outflows = connection.execute(
                _entry_balances(account.id, sign, effective_at, known_at)
            ).one()
            held = connection.execute(_held(account.id, effective_at, known_at)).scalar_one()
        posted, pending, outflows, held = (int(v) for v in (posted, pending, outflows, held))
        return Balance(
            account=code,
            currency=account.currency,
            effective_at=effective_at,
            known_at=known_at,
            posted=posted,
            pending=pending,
            held=held,
            unsettled_outflows=outflows,
            available=posted - held - outflows,
        )

    def post(self, new: NewTransaction) -> Posted:
        """Post a Transaction exactly once per Idempotency Key."""
        return self._write(
            key=new.idempotency_key,
            fingerprint=_fingerprint({"post": new.model_dump(exclude={"idempotency_key"})}),
            description=new.description,
            reverses=None,
            status=new.status,
            actor=new.actor,
            effective_at=new.effective_at,
            new_entries=new.entries,
        )

    def reverse(self, transaction_id: int, request: ReversalRequest) -> Posted:
        """Post a Reversal: a Transaction that negates every Entry of the original and
        references it. A Transaction can be reversed once, and a Reversal can't be reversed.

        The Reversal takes the original's status: a pending Transaction's Reversal is
        pending too, and settles with it (ADR 0009)."""
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
                status=original.status,
                actor=request.actor,
                effective_at=request.effective_at,
                new_entries=[
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

    def settle(self, transaction_id: int, request: SettlementRequest) -> Posted:
        """Settle a pending Transaction: from the Settlement's Effective Time, as known from
        its Recorded Time, its Entries (and its Reversal's, if any) count as Posted rather
        than Pending. No Entry is changed. A Transaction settles at most once."""
        fingerprint = _fingerprint(
            {"settle": transaction_id, **request.model_dump(exclude={"idempotency_key"})}
        )
        with self._engine.connect() as connection:
            if _replayed(connection, settlements, request.idempotency_key, fingerprint):
                return Posted(
                    transaction=_read_transaction(connection, transaction_id), replayed=True
                )
            original = _read_transaction(connection, transaction_id)
        if original.reverses is not None:
            raise NotSettleable(
                f"Transaction {transaction_id} is a Reversal; it settles with Transaction"
                f" {original.reverses}"
            )
        if original.status is not TransactionStatus.PENDING:
            raise NotSettleable(f"Transaction {transaction_id} is already posted")
        if original.reversed_by is not None:
            raise AlreadyReversed(f"Transaction {transaction_id} has been reversed")
        if request.effective_at < original.entries[0].effective_at:
            raise TooEarly(f"a Settlement can't take effect before Transaction {transaction_id}")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(settlements).values(
                        transaction_id=transaction_id,
                        idempotency_key=request.idempotency_key,
                        payload_fingerprint=fingerprint,
                        effective_at=request.effective_at,
                        recorded_at=self._clock.now(),
                        **_actor_columns(request.actor),
                    )
                )
        except IntegrityError as error:
            with self._engine.connect() as connection:
                if _replayed(connection, settlements, request.idempotency_key, fingerprint):
                    return Posted(
                        transaction=_read_transaction(connection, transaction_id), replayed=True
                    )
            if _constraint(error) == "settlements_transaction_id_key":
                raise AlreadySettled(
                    f"Transaction {transaction_id} has already been settled"
                ) from error
            raise
        return Posted(transaction=self.transaction(transaction_id), replayed=False)

    def place_hold(self, new: NewHold) -> HoldRecorded:
        """Place a Hold exactly once per Idempotency Key. It reduces the Account's Available
        Balance from its Effective Time and moves no money. It isn't refused for exceeding
        the Available Balance: deciding that is the caller's job."""
        fingerprint = _fingerprint({"hold": new.model_dump(exclude={"idempotency_key"})})
        with self._engine.begin() as connection:
            account_ids = _resolve_accounts(connection, [(new.account, new.currency)])
            hold_id: int | None = connection.scalar(
                pg_insert(holds)
                .values(
                    idempotency_key=new.idempotency_key,
                    payload_fingerprint=fingerprint,
                    account_id=account_ids[new.account],
                    currency=new.currency,
                    amount=new.amount,
                    description=new.description,
                    effective_at=new.effective_at,
                    recorded_at=self._clock.now(),
                    **_actor_columns(new.actor),
                )
                .on_conflict_do_nothing(index_elements=[holds.c.idempotency_key])
                .returning(holds.c.id)
            )
        with self._engine.connect() as connection:
            if hold_id is not None:
                return HoldRecorded(hold=_read_hold(connection, hold_id), replayed=False)
            original = _replayed(connection, holds, new.idempotency_key, fingerprint)
            assert original is not None
            return HoldRecorded(hold=_read_hold(connection, original), replayed=True)

    def hold(self, hold_id: int) -> Hold:
        with self._engine.connect() as connection:
            return _read_hold(connection, hold_id)

    def release_hold(self, hold_id: int, request: ReleaseRequest) -> HoldRecorded:
        """Release a Hold: the reservation ends at the release's Effective Time and no money
        moves. A Hold ends once, by release or by capture."""
        fingerprint = _fingerprint(
            {"release": hold_id, **request.model_dump(exclude={"idempotency_key"})}
        )
        with self._engine.connect() as connection:
            if _replayed(connection, hold_ends, request.idempotency_key, fingerprint):
                return HoldRecorded(hold=_read_hold(connection, hold_id), replayed=True)
            hold = _read_hold(connection, hold_id)
        if request.effective_at < hold.effective_at:
            raise TooEarly(f"Hold {hold_id} can't be released before it was placed")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(hold_ends).values(
                        hold_id=hold_id,
                        outcome=HoldOutcome.RELEASED,
                        idempotency_key=request.idempotency_key,
                        payload_fingerprint=fingerprint,
                        effective_at=request.effective_at,
                        recorded_at=self._clock.now(),
                        **_actor_columns(request.actor),
                    )
                )
        except IntegrityError as error:
            with self._engine.connect() as connection:
                if _replayed(connection, hold_ends, request.idempotency_key, fingerprint):
                    return HoldRecorded(hold=_read_hold(connection, hold_id), replayed=True)
            if _constraint(error) == "hold_ends_hold_id_key":
                raise HoldAlreadyEnded(f"Hold {hold_id} has already ended") from error
            raise
        return HoldRecorded(hold=self.hold(hold_id), replayed=False)

    def capture_hold(self, hold_id: int, new: NewTransaction) -> Posted:
        """Capture a Hold: post `new`, which moves the money, and end the Hold in the same
        commit, at the Transaction's Effective Time and with its Actor. The Transaction must
        have an Entry on the held Account; its amount need not equal the Hold's. Retries
        follow the Transaction's Idempotency Key."""
        hold = self.hold(hold_id)
        if all(entry.account != hold.account for entry in new.entries):
            raise CaptureMissesHold(f"capturing Hold {hold_id} must move money on {hold.account}")
        if new.effective_at < hold.effective_at:
            raise TooEarly(f"Hold {hold_id} can't be captured before it was placed")

        def end_hold(connection: Connection, transaction_id: int, recorded_at: datetime) -> None:
            connection.execute(
                insert(hold_ends).values(
                    hold_id=hold_id,
                    outcome=HoldOutcome.CAPTURED,
                    transaction_id=transaction_id,
                    effective_at=new.effective_at,
                    recorded_at=recorded_at,
                    **_actor_columns(new.actor),
                )
            )

        try:
            return self._write(
                key=new.idempotency_key,
                fingerprint=_fingerprint(
                    {"capture": hold_id, "post": new.model_dump(exclude={"idempotency_key"})}
                ),
                description=new.description,
                reverses=None,
                status=new.status,
                actor=new.actor,
                effective_at=new.effective_at,
                new_entries=new.entries,
                also=end_hold,
            )
        except IntegrityError as error:
            if _constraint(error) == "hold_ends_hold_id_key":
                raise HoldAlreadyEnded(f"Hold {hold_id} has already ended") from error
            raise

    def _write(
        self,
        *,
        key: str,
        fingerprint: str,
        description: str,
        reverses: int | None,
        status: TransactionStatus,
        actor: Actor,
        effective_at: datetime,
        new_entries: Sequence[NewEntry],
        also: Callable[[Connection, int, datetime], None] | None = None,
    ) -> Posted:
        """Insert a Transaction and its Entries, and run `also` in the same commit."""
        recorded_at = self._clock.now()
        with self._engine.begin() as connection:
            transaction_id: int | None = connection.scalar(
                pg_insert(transactions)
                .values(
                    idempotency_key=key,
                    payload_fingerprint=fingerprint,
                    description=description,
                    reverses_id=reverses,
                    status=status,
                    recorded_at=recorded_at,
                )
                .on_conflict_do_nothing(index_elements=[transactions.c.idempotency_key])
                .returning(transactions.c.id)
            )
            if transaction_id is not None:
                account_ids = _resolve_accounts(
                    connection, [(entry.account, entry.currency) for entry in new_entries]
                )
                connection.execute(
                    insert(entries),
                    [
                        {
                            "transaction_id": transaction_id,
                            "account_id": account_ids[new_entry.account],
                            "currency": new_entry.currency,
                            "amount": new_entry.amount,
                            "effective_at": effective_at,
                            "recorded_at": recorded_at,
                            **_actor_columns(actor),
                        }
                        for new_entry in new_entries
                    ],
                )
                if also is not None:
                    also(connection, transaction_id, recorded_at)
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


def _entry_balances(account_id: int, sign: int, effective_at: datetime, known_at: datetime) -> Any:
    """Posted, Pending and unsettled outflows of one Account, in its normal direction.

    An Entry is settled when its Transaction is posted, or when the Settlement of its
    Transaction (for a Reversal, of the Transaction it reverses) is known by `known_at` and
    in effect by `effective_at`. Unsettled Entries are grouped by that Transaction, so a
    pending Transaction and its Reversal net out, and each group that takes money out of
    the Account adds its net to the unsettled outflows.
    """
    base = func.coalesce(transactions.c.reverses_id, transactions.c.id)
    settled = or_(
        transactions.c.status == TransactionStatus.POSTED,
        exists().where(
            settlements.c.transaction_id == base,
            settlements.c.effective_at <= effective_at,
            settlements.c.recorded_at <= known_at,
        ),
    )
    visible = (
        select(
            base.label("base"),
            settled.label("settled"),
            func.sum(entries.c.amount * sign).label("net"),
        )
        .join(transactions, transactions.c.id == entries.c.transaction_id)
        .where(
            entries.c.account_id == account_id,
            entries.c.effective_at <= effective_at,
            entries.c.recorded_at <= known_at,
        )
        .group_by(base, settled)
        .subquery()
    )

    def total(value: ColumnElement[Any], where: ColumnElement[bool]) -> ColumnElement[Any]:
        return func.coalesce(func.sum(case((where, value), else_=literal(0))), 0)

    return select(
        total(visible.c.net, visible.c.settled),
        total(visible.c.net, ~visible.c.settled),
        total(-func.least(visible.c.net, 0), ~visible.c.settled),
    )


def _held(account_id: int, effective_at: datetime, known_at: datetime) -> Any:
    """The total of the Account's Holds placed and not yet ended, at `effective_at` as known
    at `known_at`."""
    ended = exists().where(
        hold_ends.c.hold_id == holds.c.id,
        hold_ends.c.effective_at <= effective_at,
        hold_ends.c.recorded_at <= known_at,
    )
    return select(func.coalesce(func.sum(holds.c.amount), 0)).where(
        holds.c.account_id == account_id,
        holds.c.effective_at <= effective_at,
        holds.c.recorded_at <= known_at,
        ~ended,
    )


def _replayed(connection: Connection, table: Table, key: str, fingerprint: str) -> int | None:
    """The id of the row already written with this Idempotency Key, if any. The same key
    with a different payload is a conflict."""
    original = connection.execute(
        select(table.c.id, table.c.payload_fingerprint).where(table.c.idempotency_key == key)
    ).one_or_none()
    if original is None:
        return None
    if original.payload_fingerprint != fingerprint:
        raise IdempotencyConflict(
            f"Idempotency Key {key!r} was already used with a different payload"
        )
    found: int = original.id
    return found


def _resolve_accounts(connection: Connection, legs: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Account ids by code, checking each (code, currency) names an Account in that
    currency."""
    legs = list(legs)
    codes = {code for code, _ in legs}
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
    for code, wanted in legs:
        currency = found[code].currency
        if currency != wanted:
            raise CurrencyMismatch(f"Account {code} is in {currency}, not {wanted}")
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
    settlement = connection.execute(
        select(settlements).where(
            settlements.c.transaction_id == (row.reverses_id or transaction_id)
        )
    ).one_or_none()
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
        status=TransactionStatus(row.status),
        reverses=row.reverses_id,
        reversed_by=reversed_by,
        settlement=None
        if settlement is None
        else Settlement(
            id=settlement.id,
            effective_at=settlement.effective_at,
            recorded_at=settlement.recorded_at,
            actor=_actor_from(settlement),
        ),
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


def _read_hold(connection: Connection, hold_id: int) -> Hold:
    row = connection.execute(
        select(holds, accounts.c.code.label("account_code"))
        .join(accounts, accounts.c.id == holds.c.account_id)
        .where(holds.c.id == hold_id)
    ).one_or_none()
    if row is None:
        raise HoldNotFound(f"no Hold {hold_id}")
    end = connection.execute(select(hold_ends).where(hold_ends.c.hold_id == hold_id)).one_or_none()
    return Hold(
        id=row.id,
        idempotency_key=row.idempotency_key,
        account=row.account_code,
        currency=row.currency,
        amount=row.amount,
        description=row.description,
        effective_at=row.effective_at,
        recorded_at=row.recorded_at,
        actor=_actor_from(row),
        ended=None
        if end is None
        else HoldEnd(
            outcome=HoldOutcome(end.outcome),
            transaction=end.transaction_id,
            effective_at=end.effective_at,
            recorded_at=end.recorded_at,
            actor=_actor_from(end),
        ),
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
