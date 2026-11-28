"""Auction Closes and their Payouts (SPEC §4.4, ADR 0004, ADR 0012).

Closing an auction, in one database transaction, every Ledger write through the Journal
(`Journal.within`) with the `auction-close` system job as Actor:

- each lot is one posted Transaction at the close: the buyer's Customer Balance pays the
  hammer price, the seller's is credited the price less Mintwharf's fee (SELLER_FEE_BPS,
  rounded down), and Fee Revenue the fee. Gross, never netted: a Customer who both bought
  and sold owes their purchases and is owed their sales, lot by lot;
- each seller gets one Payout of everything they're owed from the auction:
  - an Instant Payout, if they asked for one and it keeps their outstanding Instant Payout
    exposure (this one plus earlier ones whose funds aren't Available yet) within
    INSTANT_PAYOUT_LIMIT_CENTS. It goes out over RTP straight after the commit, at
    Mintwharf's risk;
  - otherwise a Standard Payout: a Hold on the seller's Customer Balance for the amount,
    so the proceeds are Posted but not Available.

The funds behind a Payout are the buyers of its seller's lots in that auction, and they are
Available when each of those buyers' Available Balance is no longer negative: their ACH
funding has been made Available by the Ledger (its Hold released), or they had the money
already. `release_payouts()`, the `payout-release` job on the Ledger's Clock, releases a
Standard Payout's Hold at that moment and sends it over ACH. It also turns a declined
Instant Payout into a Standard Payout (a Hold, then the same release), and sends any Payout
whose Payment didn't go out (the Provider was unreachable). Each step is one database
transaction, appended at most once per Payout, so the job is safe to run at any time.
"""

import logging
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, Row, and_, exists, func, insert, select

from railbench.common.clock import Clock
from railbench.common.fingerprint import fingerprint as payload_fingerprint
from railbench.ledger.journal import AccountNotFound, DuplicateAccount, Journal
from railbench.ledger.model import (
    AccountType,
    NewAccount,
    NewEntry,
    NewHold,
    NewTransaction,
    ReleaseRequest,
    SystemJob,
)
from railbench.marketplace.model import (
    AuctionClose,
    AuctionCloseRecorded,
    CustomerPosition,
    Lot,
    NewAuctionClose,
    Payout,
    PayoutPayment,
    PayoutStep,
    Speed,
    lot_key,
    payout_key,
)
from railbench.marketplace.tables import auction_closes, lots, payout_steps, payouts
from railbench.payments.model import NewPayment
from railbench.payments.provider import ProviderError
from railbench.payments.service import Payments
from railbench.payments.tables import payments as payment_rows

log = logging.getLogger(__name__)

AUCTION_CLOSE_JOB = SystemJob(name="auction-close", version="1.0.0")
PAYOUT_JOB = SystemJob(name="payout-release", version="1.0.0")
BASIS_POINTS = 10_000
# Advisory lock spaces (two-key form), apart from Payments' (4) and the Journal's.
AUCTION_LOCK_SPACE = 5
PAYOUT_LOCK_SPACE = 6


class MarketplaceError(Exception):
    """A request the marketplace refuses. The HTTP layer maps each kind to a status."""


class AuctionCloseNotFound(MarketplaceError):
    pass


class AuctionCloseConflict(MarketplaceError):
    """The auction was already closed with different lots."""


class AuctionCloseRejected(MarketplaceError):
    """Well formed, but the Ledger can't book it."""


def fee_revenue_account(currency: str) -> str:
    return f"fee-revenue:{currency.lower()}"


def seller_fee(hammer_price: int, fee_bps: int) -> int:
    """Mintwharf's fee on one lot, rounded down to the Minor Unit (in the seller's favour)."""
    return hammer_price * fee_bps // BASIS_POINTS


@dataclass(frozen=True)
class _PayoutRef:
    id: int
    auction_id: str
    seller: str
    amount: int
    currency: str

    @staticmethod
    def of(row: Row[Any]) -> "_PayoutRef":
        return _PayoutRef(row.id, row.auction_id, row.customer_account, row.amount, row.currency)


@dataclass(frozen=True)
class _Decision:
    speed: Speed
    detail: str


class Marketplace:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        journal: Journal,
        payments: Payments,
        seller_fee_bps: int,
        instant_payout_limit: int,
    ) -> None:
        if not 0 <= seller_fee_bps < BASIS_POINTS:
            raise ValueError("the seller fee must be at least 0 and under 100%")
        if instant_payout_limit < 0:
            raise ValueError("the Instant Payout limit can't be negative")
        self._engine = engine
        self._clock = clock
        self._journal = journal
        self._payments = payments
        self._fee_bps = seller_fee_bps
        self._limit = instant_payout_limit

    # Closing an auction

    def close(self, new: NewAuctionClose) -> AuctionCloseRecorded:
        """Book the Auction Close and make its Payouts, once per auction ID."""
        fingerprint = payload_fingerprint(new.model_dump(exclude={"auction_id"}))
        if self._recorded(new.auction_id, fingerprint):
            return AuctionCloseRecorded(
                auction_close=self.auction_close(new.auction_id), replayed=True
            )
        now = self._clock.now()
        if new.closed_at > now:
            raise AuctionCloseRejected("an auction can't close in the future")
        for code in sorted({c for lot in new.lots for c in (lot.buyer, lot.seller)}):
            self._check_customer_balance(code, new.currency)
        self._ensure_account(fee_revenue_account(new.currency), AccountType.REVENUE, new.currency)

        owed: dict[str, int] = {}
        for lot in new.lots:
            owed[lot.seller] = (
                owed.get(lot.seller, 0)
                + lot.hammer_price
                - seller_fee(lot.hammer_price, self._fee_bps)
            )
        with self._engine.begin() as connection:
            connection.execute(
                select(
                    func.pg_advisory_xact_lock(AUCTION_LOCK_SPACE, func.hashtext(new.auction_id))
                )
            )
            if _auction_row(connection, new.auction_id) is None:
                self._book(connection, new, fingerprint, owed, now)
        if not self._recorded(new.auction_id, fingerprint):  # pragma: no cover - a race lost
            raise AssertionError("the Auction Close was not recorded")
        self._send_unsent()
        return AuctionCloseRecorded(
            auction_close=self.auction_close(new.auction_id), replayed=False
        )

    def _book(
        self,
        connection: Connection,
        new: NewAuctionClose,
        fingerprint: str,
        owed: dict[str, int],
        now: datetime,
    ) -> None:
        journal = self._journal.within(connection)
        # Decided before this auction's lots are booked, so its own purchases don't change
        # whether earlier Instant Payouts' funds count as Available.
        decisions = {
            seller: self._decide(connection, seller, amount, seller in new.instant_payouts)
            for seller, amount in sorted(owed.items())
        }
        auction_close_id = connection.scalar(
            insert(auction_closes)
            .values(
                auction_id=new.auction_id,
                payload_fingerprint=fingerprint,
                currency=new.currency,
                closed_at=new.closed_at,
                seller_fee_bps=self._fee_bps,
                instant_payout_limit=self._limit,
                recorded_at=now,
            )
            .returning(auction_closes.c.id)
        )
        fee_revenue = fee_revenue_account(new.currency)
        for lot in new.lots:
            fee = seller_fee(lot.hammer_price, self._fee_bps)
            legs = [
                NewEntry(account=lot.buyer, currency=new.currency, amount=lot.hammer_price),
                NewEntry(
                    account=lot.seller, currency=new.currency, amount=-(lot.hammer_price - fee)
                ),
            ]
            if fee:
                legs.append(NewEntry(account=fee_revenue, currency=new.currency, amount=-fee))
            booked = journal.post(
                NewTransaction(
                    idempotency_key=lot_key(new.auction_id, lot.lot_id),
                    actor=AUCTION_CLOSE_JOB,
                    effective_at=new.closed_at,
                    description=f"Auction {new.auction_id} lot {lot.lot_id}",
                    entries=legs,
                )
            ).transaction
            connection.execute(
                insert(lots).values(
                    auction_close_id=auction_close_id,
                    lot_id=lot.lot_id,
                    buyer=lot.buyer,
                    seller=lot.seller,
                    hammer_price=lot.hammer_price,
                    fee=fee,
                    transaction_id=booked.id,
                )
            )
        for seller, amount in sorted(owed.items()):
            payout_id = connection.scalar(
                insert(payouts)
                .values(
                    auction_close_id=auction_close_id,
                    customer_account=seller,
                    currency=new.currency,
                    amount=amount,
                    requested="instant" if seller in new.instant_payouts else "standard",
                    recorded_at=now,
                )
                .returning(payouts.c.id)
            )
            decision = decisions[seller]
            if decision.speed == "instant":
                _append_step(
                    connection,
                    payout_id,
                    "instant",
                    decision.detail,
                    AUCTION_CLOSE_JOB,
                    effective_at=new.closed_at,
                    recorded_at=now,
                )
            else:
                payout = _PayoutRef(payout_id, new.auction_id, seller, amount, new.currency)
                self._hold(
                    journal,
                    connection,
                    payout,
                    decision.detail,
                    AUCTION_CLOSE_JOB,
                    effective_at=new.closed_at,
                )

    def _decide(
        self, connection: Connection, seller: str, amount: int, wants_instant: bool
    ) -> _Decision:
        if not wants_instant:
            return _Decision(
                "standard", "Standard Payout: sent once the buyers' funds are Available"
            )
        exposure = self._instant_exposure(connection, seller)
        if exposure + amount <= self._limit:
            return _Decision(
                "instant",
                f"Instant Payout over RTP at Mintwharf's risk: {amount} with {exposure} "
                f"outstanding is within the limit of {self._limit}",
            )
        return _Decision(
            "standard",
            f"Instant Payout of {amount} with {exposure} outstanding is over the limit of "
            f"{self._limit}: falls back to a Standard Payout",
        )

    def _instant_exposure(self, connection: Connection, seller: str) -> int:
        """What Mintwharf has at risk on this Customer: their Instant Payouts not declined
        whose funds aren't Available yet."""
        sent = connection.execute(
            select(payouts, auction_closes.c.auction_id)
            .join(auction_closes, auction_closes.c.id == payouts.c.auction_close_id)
            .where(payouts.c.customer_account == seller, _has_step("instant"), ~_has_step("held"))
        ).all()
        coverage: dict[str, bool] = {}
        total = 0
        for payout in sent:
            state = self._payment_state(payout_key(payout.auction_id, seller, "rtp"))
            if state == "declined" or self._funded(connection, payout, coverage):
                continue
            total += payout.amount
        return int(total)

    # The release job

    def release_payouts(self) -> int:
        """Move every Payout that's due on a step: fall back a declined Instant Payout to
        Standard, release a Standard Payout whose funds are Available, and send any Payout
        released but not yet sent. Returns how many steps it appended."""
        now = self._clock.now()
        moved = 0
        with self._engine.connect() as connection:
            instant = connection.execute(
                _payouts_with().where(_has_step("instant", by=now), ~_has_step("held"))
            ).all()
        for payout in instant:
            state = self._payment_state(
                payout_key(payout.auction_id, payout.customer_account, "rtp")
            )
            if state == "declined" and self._fall_back(payout, now):
                moved += 1
        with self._engine.connect() as connection:
            held = connection.execute(
                _payouts_with().where(_has_step("held", by=now), ~_has_step("released"))
            ).all()
            coverage: dict[str, bool] = {}
            due = [p for p in held if self._funded(connection, p, coverage)]
        for payout in due:
            if self._release(payout, now):
                moved += 1
        self._send_unsent()
        return moved

    def _fall_back(self, payout: Row[Any], now: datetime) -> bool:
        with self._engine.begin() as connection:
            _lock_payout(connection, payout.id)
            if _step_of(connection, payout.id, "held") is not None:
                return False
            self._hold(
                self._journal.within(connection),
                connection,
                _PayoutRef.of(payout),
                "Instant Payout declined over RTP: falls back to a Standard Payout",
                PAYOUT_JOB,
                effective_at=now,
            )
        return True

    def _release(self, payout: Row[Any], now: datetime) -> bool:
        with self._engine.begin() as connection:
            _lock_payout(connection, payout.id)
            if _step_of(connection, payout.id, "released") is not None:
                return False
            held = _step_of(connection, payout.id, "held")
            assert held is not None
            self._journal.within(connection).release_hold(
                held.hold_id,
                ReleaseRequest(
                    idempotency_key=payout_key(
                        payout.auction_id, payout.customer_account, "release"
                    ),
                    actor=PAYOUT_JOB,
                    effective_at=now,
                ),
            )
            _append_step(
                connection,
                payout.id,
                "released",
                "the buyers' funds are Available: sent over ACH",
                PAYOUT_JOB,
                effective_at=now,
                recorded_at=self._clock.now(),
                hold_id=held.hold_id,
            )
        return True

    def _send_unsent(self) -> None:
        """Send every Instant Payout and released Standard Payout with no Payment yet."""
        with self._engine.connect() as connection:
            ready = connection.execute(
                _payouts_with(
                    _has_step("instant").label("instant"), _has_step("released").label("released")
                ).where((_has_step("instant") & ~_has_step("held")) | _has_step("released"))
            ).all()
            wanted = {
                payout_key(p.auction_id, p.customer_account, "ach" if p.released else "rtp"): p
                for p in ready
            }
            sent = set(
                connection.scalars(
                    select(payment_rows.c.idempotency_key).where(
                        payment_rows.c.idempotency_key.in_(wanted)
                    )
                )
            )
        for key, payout in sorted(wanted.items(), key=lambda item: item[1].id):
            if key in sent:
                continue
            rail = "ach" if payout.released else "rtp"
            kind = "Standard" if payout.released else "Instant"
            try:
                self._payments.originate(
                    NewPayment(
                        idempotency_key=key,
                        rail=rail,
                        direction="push",
                        customer_account=payout.customer_account,
                        amount=payout.amount,
                        currency=payout.currency,
                        description=f"{kind} Payout for auction {payout.auction_id}",
                    )
                )
            except ProviderError:
                log.warning("Payout not sent yet", extra={"payout_id": payout.id, "rail": rail})

    # Reading

    def auction_close(self, auction_id: str) -> AuctionClose:
        with self._engine.connect() as connection:
            row = _auction_row(connection, auction_id)
            if row is None:
                raise AuctionCloseNotFound(f"no Auction Close {auction_id}")
            lot_rows = connection.execute(
                select(lots).where(lots.c.auction_close_id == row.id).order_by(lots.c.id)
            ).all()
            payout_rows = connection.execute(
                select(payouts).where(payouts.c.auction_close_id == row.id).order_by(payouts.c.id)
            ).all()
            step_rows = connection.execute(
                select(payout_steps)
                .where(payout_steps.c.payout_id.in_([p.id for p in payout_rows]))
                .order_by(payout_steps.c.id)
            ).all()
            made = {
                key: payment_id
                for key, payment_id in connection.execute(
                    select(payment_rows.c.idempotency_key, payment_rows.c.id).where(
                        payment_rows.c.idempotency_key.in_(
                            [
                                payout_key(auction_id, p.customer_account, rail)
                                for p in payout_rows
                                for rail in ("rtp", "ach")
                            ]
                        )
                    )
                )
            }
        return AuctionClose(
            auction_id=row.auction_id,
            closed_at=row.closed_at,
            currency=row.currency,
            seller_fee_bps=row.seller_fee_bps,
            instant_payout_limit=row.instant_payout_limit,
            recorded_at=row.recorded_at,
            lots=[
                Lot(
                    lot_id=lot.lot_id,
                    buyer=lot.buyer,
                    seller=lot.seller,
                    hammer_price=lot.hammer_price,
                    fee=lot.fee,
                    transaction_id=lot.transaction_id,
                )
                for lot in lot_rows
            ],
            customers=_positions(lot_rows),
            payouts=[
                self._payout(auction_id, p, [s for s in step_rows if s.payout_id == p.id], made)
                for p in payout_rows
            ],
        )

    def _payout(
        self, auction_id: str, row: Row[Any], steps: list[Row[Any]], made: dict[str, int]
    ) -> Payout:
        sent = []
        for rail in ("rtp", "ach"):
            payment_id = made.get(payout_key(auction_id, row.customer_account, rail))
            if payment_id is not None:
                state = self._payments.payment(payment_id).state
                sent.append(PayoutPayment(payment_id=payment_id, rail=rail, state=state))
        return Payout(
            id=row.id,
            customer_account=row.customer_account,
            currency=row.currency,
            amount=row.amount,
            requested=row.requested,
            speed="standard" if any(s.step == "held" for s in steps) else "instant",
            steps=[
                PayoutStep(
                    step=s.step,
                    detail=s.detail,
                    hold_id=s.hold_id,
                    job=f"{s.job_name}@{s.job_version}",
                    effective_at=s.effective_at,
                    recorded_at=s.recorded_at,
                )
                for s in steps
            ],
            payments=sent,
        )

    # Helpers

    def _hold(
        self,
        journal: Journal,
        connection: Connection,
        payout: "_PayoutRef",
        detail: str,
        job: SystemJob,
        effective_at: datetime,
    ) -> None:
        """Hold a Standard Payout's amount on the seller's Customer Balance until its funds
        are Available."""
        hold = journal.place_hold(
            NewHold(
                idempotency_key=payout_key(payout.auction_id, payout.seller, "hold"),
                actor=job,
                effective_at=effective_at,
                account=payout.seller,
                currency=payout.currency,
                amount=payout.amount,
                description=f"Standard Payout for auction {payout.auction_id}, until its "
                "funds are Available",
            )
        ).hold
        _append_step(
            connection,
            payout.id,
            "held",
            detail,
            job,
            effective_at=effective_at,
            recorded_at=self._clock.now(),
            hold_id=hold.id,
        )

    def _funded(self, connection: Connection, payout: Row[Any], coverage: dict[str, bool]) -> bool:
        """Whether the funds behind a Payout are Available: no buyer of its seller's lots in
        its auction has a negative Available Balance. `coverage` caches each buyer for one
        run."""
        buyers = connection.scalars(
            select(lots.c.buyer)
            .distinct()
            .where(
                lots.c.auction_close_id == payout.auction_close_id,
                lots.c.seller == payout.customer_account,
            )
        ).all()
        now = self._clock.now()
        for buyer in buyers:
            if buyer not in coverage:
                coverage[buyer] = (
                    self._journal.within(connection).balances(buyer, now, now).available >= 0
                )
            if not coverage[buyer]:
                return False
        return True

    def _payment_state(self, key: str) -> str | None:
        with self._engine.connect() as connection:
            payment_id = connection.scalar(
                select(payment_rows.c.id).where(payment_rows.c.idempotency_key == key)
            )
        return None if payment_id is None else str(self._payments.payment(payment_id).state)

    def _recorded(self, auction_id: str, fingerprint: str) -> bool:
        with self._engine.connect() as connection:
            row = _auction_row(connection, auction_id)
        if row is None:
            return False
        if row.payload_fingerprint != fingerprint:
            raise AuctionCloseConflict(f"auction {auction_id} was already closed differently")
        return True

    def _check_customer_balance(self, code: str, currency: str) -> None:
        try:
            account = self._journal.account(code)
        except AccountNotFound as error:
            raise AuctionCloseRejected(f"no Customer Balance {code}") from error
        if account.type is not AccountType.LIABILITY or account.currency != currency:
            raise AuctionCloseRejected(f"{code} is not a {currency} Customer Balance")

    def _ensure_account(self, code: str, type_: AccountType, currency: str) -> None:
        try:
            self._journal.account(code)
        except AccountNotFound:
            with suppress(DuplicateAccount):  # opened concurrently
                self._journal.open_account(NewAccount(code=code, type=type_, currency=currency))


def _positions(lot_rows: Iterable[Row[Any]]) -> list[CustomerPosition]:
    totals: dict[str, dict[str, int]] = {}
    for lot in lot_rows:
        buyer = totals.setdefault(lot.buyer, {"owes": 0, "sold": 0, "fees": 0})
        buyer["owes"] += lot.hammer_price
        seller = totals.setdefault(lot.seller, {"owes": 0, "sold": 0, "fees": 0})
        seller["sold"] += lot.hammer_price
        seller["fees"] += lot.fee
    return [
        CustomerPosition(customer_account=code, owed=t["sold"] - t["fees"], **t)
        for code, t in sorted(totals.items())
    ]


def _has_step(step: str, by: datetime | None = None) -> Any:
    """The Payout has this step, taking effect at or before `by` if given (a Payout isn't due
    before its Auction Close took effect)."""
    condition = and_(payout_steps.c.payout_id == payouts.c.id, payout_steps.c.step == step)
    if by is not None:
        condition = and_(condition, payout_steps.c.effective_at <= by)
    return exists().where(condition)


def _payouts_with(*columns: Any) -> Any:
    return (
        select(payouts, auction_closes.c.auction_id, *columns)
        .join(auction_closes, auction_closes.c.id == payouts.c.auction_close_id)
        .order_by(payouts.c.id)
    )


def _auction_row(connection: Connection, auction_id: str) -> Row[Any] | None:
    return connection.execute(
        select(auction_closes).where(auction_closes.c.auction_id == auction_id)
    ).one_or_none()


def _step_of(connection: Connection, payout_id: int, step: str) -> Row[Any] | None:
    return connection.execute(
        select(payout_steps).where(
            payout_steps.c.payout_id == payout_id, payout_steps.c.step == step
        )
    ).one_or_none()


def _append_step(
    connection: Connection,
    payout_id: int,
    step: str,
    detail: str,
    job: SystemJob,
    *,
    effective_at: datetime,
    recorded_at: datetime,
    hold_id: int | None = None,
) -> None:
    connection.execute(
        insert(payout_steps).values(
            payout_id=payout_id,
            step=step,
            detail=detail,
            hold_id=hold_id,
            job_name=job.name,
            job_version=job.version,
            effective_at=effective_at,
            recorded_at=recorded_at,
        )
    )


def _lock_payout(connection: Connection, payout_id: int) -> None:
    connection.execute(
        select(func.pg_advisory_xact_lock(PAYOUT_LOCK_SPACE, payout_id % 2_147_483_647))
    )
