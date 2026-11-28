"""Auction Closes and their Payouts (SPEC §4.4, ADR 0004, ADR 0012).

Closing an auction is one database transaction, every read and write on its connection and
every Ledger write through the Journal (`Journal.within`) with the `auction-close` system
job as Actor:

- each lot is one posted Transaction at the close: the buyer's Customer Balance pays the
  hammer price, the seller's is credited the price less Mintwharf's fee (SELLER_FEE_BPS,
  rounded down), and Fee Revenue the fee. Gross, never netted: a Customer who both bought
  and sold owes their purchases and is owed their sales, lot by lot;
- each seller gets one Payout of everything they're owed from the auction, and its amount
  is reserved at once by a Hold on the seller's Customer Balance. The Hold stays until the
  Transaction that books the money going out captures it (Payments does that, at an RTP
  settlement or an ACH submission), or, if the money stays, until it's released. So the
  proceeds are never Available, to the seller or as funding for what they bought, while
  they're owed out;
- a Payout is an Instant Payout if the seller asked for one, it keeps their outstanding
  Instant Payout exposure (this one plus earlier ones whose funds aren't Available yet)
  within INSTANT_PAYOUT_LIMIT_CENTS, and the seller's own Available Balance isn't negative
  (they don't owe Mintwharf for their own purchases); it goes out over RTP straight after
  the commit, at Mintwharf's risk. Otherwise it's a Standard Payout. The exposure check and
  the decision run under a lock per seller, so two auctions closing at once can't both
  spend the same limit.

The funds behind a Standard Payout are Available when none of the buyers of its seller's
lots in that auction, nor the seller, has a negative Available Balance less what they owe in
Recovery Receivable: a buyer who funded by ACH once the Ledger releases their funding's
Hold, the seller once their own purchases are paid for. A buyer whose funding was returned
(ADR 0013) holds the Payouts behind them up again, and keeps an Instant Payout's risk on its
seller's limit, until they fund what they owe. `release_payouts()`, the `payout-release`
job on the Ledger's Clock, then sends it over ACH. The job also makes a declined Instant
Payout Standard (its Hold stays), releases the Hold of a Payout whose ACH Payment ended
without moving money, and sends any Payout whose Payment didn't go out (the Provider was
unreachable). Each step is one database transaction, appended at most once per Payout, so
the job is safe to run at any time. A Standard Payout's view says who it's waiting for.
"""

import logging
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, Row, and_, exists, insert, or_, select

from railbench.common.clock import Clock
from railbench.common.fingerprint import fingerprint as payload_fingerprint
from railbench.common.locks import LockSpace, lock, lock_name
from railbench.ledger.journal import AccountNotFound, DuplicateAccount, Journal
from railbench.ledger.model import (
    AccountType,
    NewAccount,
    NewEntry,
    NewHold,
    NewTransaction,
    ReleaseRequest,
    SystemJob,
    job_label,
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
    Step,
    lot_key,
    payout_key,
)
from railbench.marketplace.tables import auction_closes, lots, payout_steps, payouts
from railbench.payments.model import NewPayment, PaymentRef
from railbench.payments.provider import ProviderError
from railbench.payments.service import Payments
from railbench.payments.state_machine import PaymentState

log = logging.getLogger(__name__)

AUCTION_CLOSE_JOB = SystemJob(name="auction-close", version="1.0.0")
PAYOUT_JOB = SystemJob(name="payout-release", version="1.0.0")
BASIS_POINTS = 10_000
MONEY_STAYED = (PaymentState.DECLINED, PaymentState.CANCELED)


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
class AuctionTerms:
    """What an Auction Close is booked under, recorded with it: the seller fee and the
    Instant Payout limit."""

    seller_fee_bps: int
    instant_payout_limit: int

    def __post_init__(self) -> None:
        if not 0 <= self.seller_fee_bps < BASIS_POINTS:
            raise ValueError("the seller fee must be at least 0 and under 100%")
        if self.instant_payout_limit < 0:
            raise ValueError("the Instant Payout limit can't be negative")


@dataclass(frozen=True)
class _SpeedChoice:
    speed: Speed
    reason: str


class Marketplace:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        journal: Journal,
        payments: Payments,
        terms: AuctionTerms | None = None,
    ) -> None:
        """`terms` are what `close()` books under unless it's given others; the release
        job doesn't need any."""
        self._engine = engine
        self._clock = clock
        self._journal = journal
        self._payments = payments
        self._terms = terms

    # Closing an auction

    def close(
        self, new: NewAuctionClose, terms: AuctionTerms | None = None
    ) -> AuctionCloseRecorded:
        """Book the Auction Close and decide its Payouts, once per auction ID, in one
        database transaction; then send its Instant Payouts."""
        terms = terms or self._terms
        if terms is None:
            raise ValueError("an Auction Close needs its seller fee and Instant Payout limit")
        fingerprint = payload_fingerprint(new.model_dump(exclude={"auction_id"}))
        now = self._clock.now()
        with self._engine.begin() as connection:
            lock_name(connection, LockSpace.AUCTION, new.auction_id)
            recorded = _auction_row(connection, new.auction_id)
            if recorded is not None and recorded.payload_fingerprint != fingerprint:
                raise AuctionCloseConflict(
                    f"auction {new.auction_id} was already closed differently"
                )
            if recorded is None:
                if new.closed_at > now:
                    raise AuctionCloseRejected("an auction can't close in the future")
                self._book(connection, new, fingerprint, terms, now)
        if recorded is None:
            self._send_unsent()
        return AuctionCloseRecorded(
            auction_close=self.auction_close(new.auction_id), replayed=recorded is not None
        )

    def _book(
        self,
        connection: Connection,
        new: NewAuctionClose,
        fingerprint: str,
        terms: AuctionTerms,
        now: datetime,
    ) -> None:
        journal = self._journal.within(connection)
        for code in sorted({c for lot in new.lots for c in (lot.buyer, lot.seller)}):
            _check_customer_balance(journal, code, new.currency)
        fee_revenue = fee_revenue_account(new.currency)
        _ensure_account(journal, fee_revenue, AccountType.REVENUE, new.currency)
        owed: dict[str, int] = {}
        for lot in new.lots:
            fee = seller_fee(lot.hammer_price, terms.seller_fee_bps)
            owed[lot.seller] = owed.get(lot.seller, 0) + lot.hammer_price - fee
        # One lock per seller asking for an Instant Payout, in sorted order (no deadlocks),
        # held to the commit: no other Auction Close can spend their limit meanwhile.
        for seller in new.instant_payouts:
            lock_name(connection, LockSpace.SELLER, seller)
        # Measured before this auction's lots are booked, so its purchases don't change
        # whether earlier Instant Payouts' funds count as Available.
        exposure = {
            seller: self._instant_exposure(connection, seller) for seller in new.instant_payouts
        }

        auction_close_id = connection.scalar(
            insert(auction_closes)
            .values(
                auction_id=new.auction_id,
                payload_fingerprint=fingerprint,
                currency=new.currency,
                closed_at=new.closed_at,
                seller_fee_bps=terms.seller_fee_bps,
                instant_payout_limit=terms.instant_payout_limit,
                recorded_at=now,
            )
            .returning(auction_closes.c.id)
        )
        for lot in new.lots:
            fee = seller_fee(lot.hammer_price, terms.seller_fee_bps)
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
        payout_ids = {}
        for seller, amount in sorted(owed.items()):
            payout_ids[seller] = connection.scalar(
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
            hold = journal.place_hold(
                NewHold(
                    idempotency_key=payout_key(new.auction_id, seller, "hold"),
                    actor=AUCTION_CLOSE_JOB,
                    effective_at=new.closed_at,
                    account=seller,
                    currency=new.currency,
                    amount=amount,
                    description=f"Payout for auction {new.auction_id}, reserved until it goes out",
                )
            ).hold
            _append_step(
                connection,
                payout_ids[seller],
                "reserved",
                "the Payout's amount is reserved until it goes out",
                AUCTION_CLOSE_JOB,
                effective_at=new.closed_at,
                recorded_at=now,
                hold_id=hold.id,
            )
        # Decided once every lot and reservation is booked, so the seller's own Available
        # Balance counts what they bought here.
        for seller, amount in sorted(owed.items()):
            choice = self._choose_speed(connection, seller, amount, exposure.get(seller), terms)
            _append_step(
                connection,
                payout_ids[seller],
                choice.speed,
                choice.reason,
                AUCTION_CLOSE_JOB,
                effective_at=new.closed_at,
                recorded_at=now,
            )

    def _choose_speed(
        self,
        connection: Connection,
        seller: str,
        amount: int,
        exposure: int | None,
        terms: AuctionTerms,
    ) -> _SpeedChoice:
        """Instant if asked for (`exposure` is measured only then), within the limit, and the
        seller doesn't owe Mintwharf; Standard otherwise."""
        if exposure is None:
            return _SpeedChoice(
                "standard", "Standard Payout: sent once the funds behind it are Available"
            )
        limit = terms.instant_payout_limit
        if exposure + amount > limit:
            return _SpeedChoice(
                "standard",
                f"Instant Payout of {amount} with {exposure} outstanding is over the limit of "
                f"{limit}: falls back to a Standard Payout",
            )
        if self._available(connection, seller) < 0:
            return _SpeedChoice(
                "standard",
                "the seller's own Available Balance is negative (they owe for what they "
                "bought): falls back to a Standard Payout",
            )
        return _SpeedChoice(
            "instant",
            f"Instant Payout over RTP at Mintwharf's risk: {amount} with {exposure} "
            f"outstanding is within the limit of {limit}",
        )

    def _instant_exposure(self, connection: Connection, seller: str) -> int:
        """What Mintwharf has at risk on this Customer: their Instant Payouts not declined
        whose buyers' funds aren't Available yet."""
        sent = connection.execute(
            _payouts_with().where(
                payouts.c.customer_account == seller,
                _has_step("instant"),
                ~_has_step("standard"),
            )
        ).all()
        rtp = self._payments.by_keys([_payment_key(p, "rtp") for p in sent], connection)
        available: dict[str, int] = {}
        total = 0
        for payout in sent:
            payment = rtp.get(_payment_key(payout, "rtp"))
            if payment is not None and payment.state is PaymentState.DECLINED:
                continue
            if self._holding_up(connection, payout, available, include_seller=False):
                total += payout.amount
        return total

    # The release job

    def release_payouts(self) -> int:
        """Move every Payout that's due on a step: make a declined Instant Payout Standard,
        release a Standard Payout whose funds are Available, return the reservation of one
        whose ACH Payment ended without moving money, and send any Payout not yet sent.
        Returns how many steps it appended."""
        now = self._clock.now()
        moved = 0
        with self._engine.connect() as connection:
            instant = connection.execute(
                _payouts_with().where(_has_step("instant", by=now), ~_has_step("standard"))
            ).all()
            rtp = self._payments.by_keys([_payment_key(p, "rtp") for p in instant], connection)
        for payout in instant:
            payment = rtp.get(_payment_key(payout, "rtp"))
            if payment is not None and payment.state is PaymentState.DECLINED:
                reason = "Instant Payout declined over RTP: falls back to a Standard Payout"
                moved += self._step_once(payout, "standard", reason, now)

        with self._engine.connect() as connection:
            waiting = connection.execute(
                _payouts_with().where(_has_step("standard", by=now), ~_has_step("released"))
            ).all()
            available: dict[str, int] = {}
            due = [p for p in waiting if not self._holding_up(connection, p, available)]
        for payout in due:
            moved += self._step_once(payout, "released", "the funds behind it are Available", now)

        with self._engine.connect() as connection:
            released = connection.execute(
                _payouts_with().where(_has_step("released"), ~_has_step("returned"))
            ).all()
            ach = self._payments.by_keys([_payment_key(p, "ach") for p in released], connection)
        for payout in released:
            payment = ach.get(_payment_key(payout, "ach"))
            if payment is not None and payment.state in MONEY_STAYED:
                moved += self._return(payout, now)

        self._send_unsent()
        return moved

    def _step_once(self, payout: Row[Any], step: Step, reason: str, now: datetime) -> bool:
        with self._engine.begin() as connection:
            lock(connection, LockSpace.PAYOUT, payout.id)
            if _step_of(connection, payout.id, step) is not None:
                return False
            _append_step(
                connection, payout.id, step, reason, PAYOUT_JOB, effective_at=now, recorded_at=now
            )
        return True

    def _return(self, payout: Row[Any], now: datetime) -> bool:
        """The ACH Payment ended without moving money. If it never got as far as booking its
        outflow, its reservation is still open: release it, and the money is Available."""
        with self._engine.begin() as connection:
            lock(connection, LockSpace.PAYOUT, payout.id)
            if _step_of(connection, payout.id, "returned") is not None:
                return False
            journal = self._journal.within(connection)
            reason = "its ACH Payment was reversed: the money stays in the Customer Balance"
            if journal.hold(payout.hold_id).ended is None:
                journal.release_hold(
                    payout.hold_id,
                    ReleaseRequest(
                        idempotency_key=_payment_key(payout, "return"),
                        actor=PAYOUT_JOB,
                        effective_at=now,
                    ),
                )
                reason = "its ACH Payment ended before it was submitted: reservation released"
            _append_step(
                connection,
                payout.id,
                "returned",
                reason,
                PAYOUT_JOB,
                effective_at=now,
                recorded_at=now,
            )
        return True

    def _send_unsent(self) -> None:
        """Send every Instant Payout and released Standard Payout with no Payment yet, each
        naming its reservation, which the Transaction booking its outflow captures."""
        with self._engine.connect() as connection:
            ready = connection.execute(
                _payouts_with(_has_step("released").label("released")).where(
                    or_(_has_step("instant") & ~_has_step("standard"), _has_step("released"))
                )
            ).all()
            wanted = {_payment_key(p, "ach" if p.released else "rtp"): p for p in ready}
            sent = self._payments.by_keys(wanted, connection)
        for key, payout in wanted.items():
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
                        hold_id=payout.hold_id,
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
                _payouts_with().where(payouts.c.auction_close_id == row.id)
            ).all()
            step_rows = connection.execute(
                select(payout_steps)
                .where(payout_steps.c.payout_id.in_([p.id for p in payout_rows]))
                .order_by(payout_steps.c.id)
            ).all()
            payments_by_key = self._payments.by_keys(
                [_payment_key(p, rail) for p in payout_rows for rail in ("rtp", "ach")],
                connection,
            )
            available: dict[str, int] = {}
            views = []
            for payout in payout_rows:
                steps = [s for s in step_rows if s.payout_id == payout.id]
                names = {s.step for s in steps}
                waiting_for = (
                    self._holding_up(connection, payout, available)
                    if "standard" in names and "released" not in names
                    else []
                )
                views.append(_payout(payout, steps, payments_by_key, waiting_for))
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
            payouts=views,
        )

    # Helpers

    def _holding_up(
        self,
        connection: Connection,
        payout: Row[Any],
        available: dict[str, int],
        include_seller: bool = True,
    ) -> list[str]:
        """The Customer Balances keeping the funds behind a Payout from being Available: the
        buyers of its seller's lots in its auction, and (unless left out) the seller, whose
        Available Balance is negative now. `available` caches balances for one pass."""
        buyers = connection.scalars(
            select(lots.c.buyer)
            .distinct()
            .where(
                lots.c.auction_close_id == payout.auction_close_id,
                lots.c.seller == payout.customer_account,
            )
        ).all()
        checked = sorted(set(buyers) | ({payout.customer_account} if include_seller else set()))
        for code in checked:
            if code not in available:
                available[code] = self._available(connection, code)
        return [code for code in checked if available[code] < 0]

    def _available(self, connection: Connection, code: str) -> int:
        """The Customer Balance's Available Balance less what the Customer owes Mintwharf in
        Recovery Receivable (ADR 0013): a buyer whose funding was returned owes for their
        lots still, though the deficit moved out of their Customer Balance."""
        now = self._clock.now()
        available = self._journal.within(connection).balances(code, now, now).available
        return available - self._payments.owed(code, connection)


def _payout(
    row: Row[Any],
    steps: list[Row[Any]],
    payments_by_key: dict[str, PaymentRef],
    waiting_for: list[str],
) -> Payout:
    sent = [
        PayoutPayment(payment_id=payment.id, rail=rail, state=payment.state)
        for rail in ("rtp", "ach")
        if (payment := payments_by_key.get(_payment_key(row, rail))) is not None
    ]
    return Payout(
        id=row.id,
        customer_account=row.customer_account,
        currency=row.currency,
        amount=row.amount,
        requested=row.requested,
        speed="standard" if any(s.step == "standard" for s in steps) else "instant",
        steps=[
            PayoutStep(
                step=s.step,
                detail=s.detail,
                hold_id=s.hold_id,
                job=job_label(s.job_name, s.job_version),
                effective_at=s.effective_at,
                recorded_at=s.recorded_at,
            )
            for s in steps
        ],
        payments=sent,
        waiting_for=waiting_for,
    )


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


def _payment_key(payout: Row[Any], step: str) -> str:
    return payout_key(payout.auction_id, payout.customer_account, step)


def _check_customer_balance(journal: Journal, code: str, currency: str) -> None:
    try:
        account = journal.account(code)
    except AccountNotFound as error:
        raise AuctionCloseRejected(f"no Customer Balance {code}") from error
    if account.type is not AccountType.LIABILITY or account.currency != currency:
        raise AuctionCloseRejected(f"{code} is not a {currency} Customer Balance")


def _ensure_account(journal: Journal, code: str, type_: AccountType, currency: str) -> None:
    try:
        journal.account(code)
    except AccountNotFound:
        with suppress(DuplicateAccount):  # opened concurrently
            journal.open_account(NewAccount(code=code, type=type_, currency=currency))


def _has_step(step: Step, by: datetime | None = None) -> Any:
    """The Payout has this step, taking effect at or before `by` if given (a Payout isn't due
    before its Auction Close took effect)."""
    condition = and_(payout_steps.c.payout_id == payouts.c.id, payout_steps.c.step == step)
    if by is not None:
        condition = and_(condition, payout_steps.c.effective_at <= by)
    return exists().where(condition)


def _payouts_with(*columns: Any) -> Any:
    """Payouts with their auction ID and the Hold reserving their amount."""
    reservation = (
        select(payout_steps.c.hold_id)
        .where(payout_steps.c.payout_id == payouts.c.id, payout_steps.c.step == "reserved")
        .scalar_subquery()
    )
    return (
        select(payouts, auction_closes.c.auction_id, reservation.label("hold_id"), *columns)
        .join(auction_closes, auction_closes.c.id == payouts.c.auction_close_id)
        .order_by(payouts.c.id)
    )


def _auction_row(connection: Connection, auction_id: str) -> Row[Any] | None:
    return connection.execute(
        select(auction_closes).where(auction_closes.c.auction_id == auction_id)
    ).one_or_none()


def _step_of(connection: Connection, payout_id: int, step: Step) -> Row[Any] | None:
    return connection.execute(
        select(payout_steps).where(
            payout_steps.c.payout_id == payout_id, payout_steps.c.step == step
        )
    ).one_or_none()


def _append_step(
    connection: Connection,
    payout_id: int,
    step: Step,
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
