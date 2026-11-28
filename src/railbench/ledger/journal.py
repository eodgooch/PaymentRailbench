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

`Journal.within(connection)` runs the same operations as one unit of work on a caller's
open transaction (each write in a savepoint), so a caller such as the Inbox can commit its
own rows and the Ledger writes they cause together, or neither.
"""

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    Row,
    Select,
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
from railbench.common.fingerprint import fingerprint as payload_fingerprint
from railbench.ledger.model import (
    Account,
    AccountType,
    Actor,
    Balances,
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
    def __init__(self, engine: Engine, clock: Clock, connection: Connection | None = None) -> None:
        self._engine = engine
        self._clock = clock
        self._connection = connection

    def within(self, connection: Connection) -> "Journal":
        """This Journal as a unit of work on the caller's `connection`, inside a transaction
        the caller began. Every write runs in a savepoint and every read sees what the caller
        has written so far; nothing commits until the caller commits, so the caller's own
        rows and the Ledger writes commit or roll back together. A refused write rolls back
        its own savepoint and leaves the caller's transaction usable. The deferred balance
        check runs at the caller's commit."""
        return Journal(self._engine, self._clock, connection)

    @contextmanager
    def _reading(self) -> Iterator[Connection]:
        if self._connection is None:
            with self._engine.connect() as connection:
                yield connection
        else:
            yield self._connection

    @contextmanager
    def _writing(self) -> Iterator[Connection]:
        if self._connection is None:
            with self._engine.begin() as connection:
                yield connection
        else:
            with self._connection.begin_nested():
                yield self._connection

    def open_account(self, new: NewAccount) -> Account:
        values = {**new.model_dump(), "recorded_at": self._clock.now()}
        try:
            with self._writing() as connection:
                connection.execute(insert(accounts).values(values))
        except IntegrityError as error:
            if _constraint(error) == "accounts_code_key":
                raise DuplicateAccount(f"Account {new.code} already exists") from error
            raise
        return Account.model_validate(values)

    def account(self, code: str) -> Account:
        with self._reading() as connection:
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

    def balances(
        self, code: str, effective_at: datetime, known_at: datetime | None = None
    ) -> Balances:
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
        with self._reading() as connection:
            account = connection.execute(
                select(accounts.c.id, accounts.c.type, accounts.c.currency).where(
                    accounts.c.code == code
                )
            ).one_or_none()
            if account is None:
                raise AccountNotFound(f"no Account {code}")
            sign = AccountType(account.type).normal_sign
            sums = connection.execute(
                _entry_balances(account.id, sign, effective_at, known_at)
            ).one()
            held_sum = connection.execute(_held(account.id, effective_at, known_at)).scalar_one()
        posted, pending, outflows, held = (int(v) for v in (*sums, held_sum))
        return Balances(
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
            fingerprint=payload_fingerprint({"post": new.model_dump(exclude={"idempotency_key"})}),
            description=new.description,
            reverses=None,
            status=lambda _connection: new.status,
            actor=new.actor,
            effective_at=new.effective_at,
            new_entries=new.entries,
        )

    def reverse(self, transaction_id: int, request: ReversalRequest) -> Posted:
        """Post a Reversal: a Transaction that negates every Entry of the original and
        references it. A Transaction can be reversed once, and a Reversal can't be reversed.
        A Reversal can't take effect before its original.

        Reversing a posted or settled Transaction posts the Reversal; for a settled one (a
        Return) it can't take effect before the Settlement either. Reversing a pending
        Transaction that hasn't settled (a decline) is itself pending: the two net to zero
        and neither can settle afterwards (ADR 0009). Settling and reversing one Transaction
        take the same lock, so they can't race."""
        original = self.transaction(transaction_id)
        if original.reverses is not None:
            raise CannotReverseReversal(
                f"Transaction {transaction_id} is a Reversal; post a new Transaction instead"
            )
        if request.effective_at < original.entries[0].effective_at:
            raise TooEarly(f"a Reversal can't take effect before Transaction {transaction_id}")

        def status(connection: Connection) -> TransactionStatus:
            _lock(connection, transaction_id)
            if original.status is TransactionStatus.POSTED:
                return TransactionStatus.POSTED
            settled_at = connection.scalar(
                select(settlements.c.effective_at).where(
                    settlements.c.transaction_id == transaction_id
                )
            )
            if settled_at is None:
                return TransactionStatus.PENDING
            if request.effective_at < settled_at:
                raise TooEarly(
                    f"a Return can't take effect before Transaction {transaction_id} settled"
                )
            return TransactionStatus.POSTED

        payload = {"reverse": transaction_id, **request.model_dump(exclude={"idempotency_key"})}
        try:
            return self._write(
                key=request.idempotency_key,
                fingerprint=payload_fingerprint(payload),
                description=request.description,
                reverses=transaction_id,
                status=status,
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
        with self._reading() as connection:
            return _read_transaction(connection, transaction_id)

    def settle(self, transaction_id: int, request: SettlementRequest) -> Posted:
        """Settle a pending Transaction: from the Settlement's Effective Time, as known from
        its Recorded Time, its Entries count as Posted rather than Pending. No Entry is
        changed. A Transaction settles at most once, and not once it has been reversed."""
        fingerprint = payload_fingerprint(
            {"settle": transaction_id, **request.model_dump(exclude={"idempotency_key"})}
        )

        def write(connection: Connection) -> None:
            _lock(connection, transaction_id)
            original = _read_transaction(connection, transaction_id)
            if original.reverses is not None:
                raise NotSettleable(f"Transaction {transaction_id} is a Reversal")
            if original.status is not TransactionStatus.PENDING:
                raise NotSettleable(f"Transaction {transaction_id} is already posted")
            if original.reversed_by is not None:
                raise AlreadyReversed(f"Transaction {transaction_id} has been reversed")
            if request.effective_at < original.entries[0].effective_at:
                raise TooEarly(
                    f"a Settlement can't take effect before Transaction {transaction_id}"
                )
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

        written = self._once(
            settlements,
            request.idempotency_key,
            fingerprint,
            write,
            conflicts={
                "settlements_transaction_id_key": AlreadySettled(
                    f"Transaction {transaction_id} has already been settled"
                )
            },
        )
        return Posted(transaction=self.transaction(transaction_id), replayed=not written)

    def place_hold(self, new: NewHold) -> HoldRecorded:
        """Place a Hold exactly once per Idempotency Key. It reduces the Account's Available
        Balance from its Effective Time and moves no money. It isn't refused for exceeding
        the Available Balance: deciding that is the caller's job."""
        fingerprint = payload_fingerprint({"hold": new.model_dump(exclude={"idempotency_key"})})
        with self._writing() as connection:
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
        with self._reading() as connection:
            if hold_id is not None:
                return HoldRecorded(hold=_read_hold(connection, hold_id), replayed=False)
            original = _replayed(connection, holds, new.idempotency_key, fingerprint)
            assert original is not None
            return HoldRecorded(hold=_read_hold(connection, original), replayed=True)

    def hold(self, hold_id: int) -> Hold:
        with self._reading() as connection:
            return _read_hold(connection, hold_id)

    def release_hold(self, hold_id: int, request: ReleaseRequest) -> HoldRecorded:
        """Release a Hold: the reservation ends at the release's Effective Time and no money
        moves. A Hold ends once, by release or by capture."""
        fingerprint = payload_fingerprint(
            {"release": hold_id, **request.model_dump(exclude={"idempotency_key"})}
        )

        def write(connection: Connection) -> None:
            hold = _read_hold(connection, hold_id)
            if request.effective_at < hold.effective_at:
                raise TooEarly(f"Hold {hold_id} can't be released before it was placed")
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

        written = self._once(
            hold_ends,
            request.idempotency_key,
            fingerprint,
            write,
            conflicts={
                "hold_ends_hold_id_key": HoldAlreadyEnded(f"Hold {hold_id} has already ended")
            },
        )
        return HoldRecorded(hold=self.hold(hold_id), replayed=not written)

    def capture_hold(self, hold_id: int, new: NewTransaction) -> Posted:
        """Capture a Hold: post `new`, which moves the money, and end the Hold in the same
        commit, at the Transaction's Effective Time and with its Actor. The Transaction must
        take money out of the held Account (reduce its balance, in its normal direction);
        the amount need not equal the Hold's. Retries follow the Transaction's Idempotency
        Key."""
        hold = self.hold(hold_id)
        sign = self.account(hold.account).type.normal_sign
        moved = sum(entry.amount for entry in new.entries if entry.account == hold.account)
        if moved * sign >= 0:
            raise CaptureMissesHold(
                f"capturing Hold {hold_id} must take money out of {hold.account}"
            )
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
                fingerprint=payload_fingerprint(
                    {"capture": hold_id, "post": new.model_dump(exclude={"idempotency_key"})}
                ),
                description=new.description,
                reverses=None,
                status=lambda _connection: new.status,
                actor=new.actor,
                effective_at=new.effective_at,
                new_entries=new.entries,
                on_posted=end_hold,
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
        status: Callable[[Connection], TransactionStatus],
        actor: Actor,
        effective_at: datetime,
        new_entries: Sequence[NewEntry],
        on_posted: Callable[[Connection, int, datetime], None] | None = None,
    ) -> Posted:
        """Insert a Transaction and its Entries once per Idempotency Key. `status` runs
        first, inside the commit, so it can take a lock and read before deciding; `on_posted`
        runs after the Entries, in the same commit, only when the Transaction is new."""
        recorded_at = self._clock.now()
        with self._writing() as connection:
            decided = status(connection)
            transaction_id: int | None = connection.scalar(
                pg_insert(transactions)
                .values(
                    idempotency_key=key,
                    payload_fingerprint=fingerprint,
                    description=description,
                    reverses_id=reverses,
                    status=decided,
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
                if on_posted is not None:
                    on_posted(connection, transaction_id, recorded_at)
        # Read after commit, once the deferred balance check has passed (within a unit of
        # work, after the savepoint: the check runs at the caller's commit).
        with self._reading() as connection:
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

    def _once(
        self,
        table: Table,
        key: str,
        fingerprint: str,
        write: Callable[[Connection], None],
        conflicts: dict[str, Conflict],
    ) -> bool:
        """Run `write` in one commit, once per Idempotency Key in `table`. Returns False for
        a retry of the same payload, which writes nothing. A unique violation named in
        `conflicts` (an "at most once" rule) raises that Conflict, unless it came from a
        concurrent retry of this same request."""
        with self._reading() as connection:
            if _replayed(connection, table, key, fingerprint):
                return False
        try:
            with self._writing() as connection:
                write(connection)
        except IntegrityError as error:
            with self._reading() as connection:
                if _replayed(connection, table, key, fingerprint):
                    return False
            conflict = conflicts.get(_constraint(error) or "")
            if conflict is not None:
                raise conflict from error
            raise
        return True


def _entry_balances(
    account_id: int, sign: int, effective_at: datetime, known_at: datetime
) -> Select[Decimal, Decimal, Decimal]:
    """Posted, Pending and unsettled outflows of one Account, in its normal direction
    (Postgres sums BIGINTs as NUMERIC, hence Decimal).

    An Entry is settled when its Transaction is posted, or when its Transaction's
    Settlement is known by `known_at` and in effect by `effective_at`. Unsettled Entries are
    grouped by Transaction, with a pending Reversal (a decline) in its original's group, so
    a declined Transaction nets out; each group that takes money out of the Account adds
    its net to the unsettled outflows.
    """
    base = func.coalesce(transactions.c.reverses_id, transactions.c.id)
    settled = or_(
        transactions.c.status == TransactionStatus.POSTED,
        exists().where(
            settlements.c.transaction_id == transactions.c.id,
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


def _held(account_id: int, effective_at: datetime, known_at: datetime) -> Select[Decimal]:
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


def _lock(connection: Connection, transaction_id: int) -> None:
    """Serialise settling and reversing one Transaction until this commit ends. An advisory
    lock, since row locks need UPDATE, which the app role doesn't have. It's keyed by the
    Transaction id alone: the Ledger takes no other advisory locks."""
    connection.execute(select(func.pg_advisory_xact_lock(transaction_id)))


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
        select(settlements).where(settlements.c.transaction_id == transaction_id)
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


def _constraint(error: IntegrityError) -> str | None:
    diag = getattr(error.orig, "diag", None)
    name: str | None = getattr(diag, "constraint_name", None)
    return name
