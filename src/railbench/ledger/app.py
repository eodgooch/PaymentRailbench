"""The Ledger API: a thin HTTP layer over the Journal.

Posting is idempotent. The first post of an Idempotency Key answers 201 Created with a
Location; a retry with the same payload answers 200 OK with the original Transaction and
`Idempotent-Replayed: true`; the same key with a different payload answers 409 Conflict.
Reversals, Settlements, Holds and their releases and captures work the same way; a second
Reversal, Settlement, release or capture of the same thing is a 409.

Balances are read at an Effective Time, as known at a Recorded Time (default now): both are
query parameters and must carry a UTC offset.

Payments (`railbench.payments`) are served here too: `POST /payments` asks a Provider for an
ACH pull, `GET /payments/{id}` reads one, and `POST /providers/{provider}/events` is the
webhook receiver for Provider Events. `GET /return-exceptions` lists the exceptions raised
when a Return left a Customer Balance short (optionally for one `customer_account`); a
Payment's own are on `GET /payments/{id}` too. The receiver answers 401 to a webhook whose signature
doesn't verify, 400 to a verified body it can't read, and 503 (retry later) to an event for a
Payment it hasn't recorded yet.

Auction Closes (`railbench.marketplace`): `POST /auction-closes` books one auction's lots and
makes its sellers' Payouts, once per auction ID (201, a retry 200, different lots 409), and
`GET /auction-closes/{auction_id}` reads it back with each Payout's steps and Payments. A
Payout is never made through `POST /payments`, which only makes pulls.

Settlement Reports (`railbench.payments.reports`, ADR 0014): the Ledger's jobs fetch each due
report through the Provider interface and ingest it; `GET /settlement-reports/{provider}/
{date}` lists every report received for that day, with its outcome, its lines as stated and
what was done with each Provider Adjustment. A Payment's booked adjustments are on
`GET /payments/{id}` too.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy import Engine

from railbench.common.clock import Clock
from railbench.common.service import create_service_app
from railbench.common.settings import Settings
from railbench.common.signing import BadSignature
from railbench.common.ticker import ticking
from railbench.ledger.journal import Conflict, Journal, LedgerError, NotFound, Rejected
from railbench.ledger.model import (
    Account,
    Balances,
    Hold,
    NewAccount,
    NewHold,
    NewTransaction,
    Posted,
    ReleaseRequest,
    ReversalRequest,
    SettlementRequest,
    Transaction,
)
from railbench.marketplace.model import AuctionClose, NewAuctionClose
from railbench.marketplace.service import (
    AuctionCloseConflict,
    AuctionCloseNotFound,
    AuctionTerms,
    Marketplace,
    MarketplaceError,
)
from railbench.payments.model import (
    NewPayment,
    Payment,
    Receipt,
    ReturnException,
    SettlementReportView,
)
from railbench.payments.provider import MalformedEvent, Provider, ProviderError, SimulatorProvider
from railbench.payments.reports import SettlementReports
from railbench.payments.service import (
    NotACustomerBalance,
    PaymentConflict,
    PaymentNotFound,
    PaymentNotYetKnown,
    Payments,
    PaymentsError,
    UnknownProvider,
)

log = logging.getLogger(__name__)

ERROR_STATUS = {
    NotFound: status.HTTP_404_NOT_FOUND,
    Conflict: status.HTTP_409_CONFLICT,
    Rejected: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


PAYMENTS_ERROR_STATUS = {
    PaymentNotFound: status.HTTP_404_NOT_FOUND,
    UnknownProvider: status.HTTP_404_NOT_FOUND,
    PaymentConflict: status.HTTP_409_CONFLICT,
    NotACustomerBalance: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PaymentNotYetKnown: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def error_status(error: LedgerError) -> int:
    """The HTTP status for a refusal. A LedgerError with no mapping of its own is still a
    refusal of this request, never a server fault, so it answers 422 rather than 500."""
    return next(
        (code for kind, code in ERROR_STATUS.items() if isinstance(error, kind)),
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


def get_journal(request: Request) -> Journal:
    journal: Journal = request.app.state.journal
    return journal


JournalDep = Annotated[Journal, Depends(get_journal)]


def get_payments(request: Request) -> Payments:
    payments: Payments = request.app.state.payments
    return payments


PaymentsDep = Annotated[Payments, Depends(get_payments)]


def get_marketplace(request: Request) -> Marketplace:
    marketplace: Marketplace = request.app.state.marketplace
    return marketplace


MarketplaceDep = Annotated[Marketplace, Depends(get_marketplace)]

MARKETPLACE_ERROR_STATUS = {
    AuctionCloseNotFound: status.HTTP_404_NOT_FOUND,
    AuctionCloseConflict: status.HTTP_409_CONFLICT,
}


def create_app(
    engine: Engine,
    clock: Clock,
    settings: Settings | None = None,
    provider: Provider | None = None,
) -> FastAPI:
    """The Ledger API. `provider` defaults to the Simulator at SIMULATOR_URL."""
    settings = settings or Settings()
    if provider is None:
        provider = SimulatorProvider(
            settings.simulator_url, settings.webhook_signing_secret.get_secret_value()
        )
    journal = Journal(engine, clock)
    payments = Payments(engine, clock, journal, provider)
    reports = SettlementReports(engine, clock, journal, provider)
    marketplace = Marketplace(
        engine,
        clock,
        journal,
        payments,
        AuctionTerms(
            seller_fee_bps=settings.seller_fee_bps,
            instant_payout_limit=settings.instant_payout_limit_cents,
        ),
    )

    def jobs() -> None:
        """The Ledger's Clock-driven work, in order: ACH funds become Available, then the
        Standard Payouts they were behind are released, then due Settlement Reports are
        ingested."""
        payments.make_available()
        marketplace.release_payouts()
        reports.ingest_due()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        with ticking("ledger-jobs", jobs, settings.ledger_tick_seconds):
            yield

    app = create_service_app("ledger", engine, clock, lifespan=lifespan)
    app.state.journal = journal
    app.state.payments = payments
    app.state.marketplace = marketplace
    app.state.reports = reports
    app.state.provider = provider

    @app.exception_handler(LedgerError)
    def ledger_error(_request: Request, error: LedgerError) -> JSONResponse:
        return JSONResponse(status_code=error_status(error), content={"detail": str(error)})

    @app.exception_handler(PaymentsError)
    def payments_error(_request: Request, error: PaymentsError) -> JSONResponse:
        code = PAYMENTS_ERROR_STATUS.get(type(error), status.HTTP_422_UNPROCESSABLE_CONTENT)
        return JSONResponse(status_code=code, content={"detail": str(error)})

    @app.exception_handler(MarketplaceError)
    def marketplace_error(_request: Request, error: MarketplaceError) -> JSONResponse:
        code = MARKETPLACE_ERROR_STATUS.get(type(error), status.HTTP_422_UNPROCESSABLE_CONTENT)
        return JSONResponse(status_code=code, content={"detail": str(error)})

    @app.exception_handler(ProviderError)
    def provider_error(_request: Request, error: ProviderError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(error)})

    @app.post("/accounts", status_code=status.HTTP_201_CREATED)
    def open_account(new: NewAccount, journal: JournalDep) -> Account:
        return journal.open_account(new)

    @app.get("/accounts/{code}")
    def get_account(code: str, journal: JournalDep) -> Account:
        return journal.account(code)

    @app.get("/accounts/{code}/balances")
    def get_balances(
        code: str,
        journal: JournalDep,
        effective_at: Annotated[AwareDatetime, Query()],
        known_at: Annotated[AwareDatetime | None, Query()] = None,
    ) -> Balances:
        return journal.balances(code, effective_at, known_at)

    @app.post(
        "/transactions",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "A retry: the original Transaction", "model": Transaction}},
    )
    def post_transaction(
        new: NewTransaction, journal: JournalDep, response: Response
    ) -> Transaction:
        return _respond_posted(journal.post(new), response)

    @app.get("/transactions/{transaction_id}")
    def get_transaction(transaction_id: int, journal: JournalDep) -> Transaction:
        return journal.transaction(transaction_id)

    @app.post(
        "/transactions/{transaction_id}/reversal",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "A retry: the original Reversal", "model": Transaction}},
    )
    def reverse_transaction(
        transaction_id: int, request: ReversalRequest, journal: JournalDep, response: Response
    ) -> Transaction:
        return _respond_posted(journal.reverse(transaction_id, request), response)

    @app.post(
        "/transactions/{transaction_id}/settlement",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "A retry: the settled Transaction", "model": Transaction}},
    )
    def settle_transaction(
        transaction_id: int, request: SettlementRequest, journal: JournalDep, response: Response
    ) -> Transaction:
        posted = journal.settle(transaction_id, request)
        return _respond(posted.replayed, posted.transaction, response)

    @app.post(
        "/holds",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "A retry: the original Hold", "model": Hold}},
    )
    def place_hold(new: NewHold, journal: JournalDep, response: Response) -> Hold:
        recorded = journal.place_hold(new)
        return _respond(
            recorded.replayed, recorded.hold, response, location=f"/holds/{recorded.hold.id}"
        )

    @app.get("/holds/{hold_id}")
    def get_hold(hold_id: int, journal: JournalDep) -> Hold:
        return journal.hold(hold_id)

    @app.post(
        "/holds/{hold_id}/release",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "A retry: the released Hold", "model": Hold}},
    )
    def release_hold(
        hold_id: int, request: ReleaseRequest, journal: JournalDep, response: Response
    ) -> Hold:
        released = journal.release_hold(hold_id, request)
        return _respond(released.replayed, released.hold, response)

    @app.post(
        "/holds/{hold_id}/capture",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "A retry: the capture", "model": Transaction}},
    )
    def capture_hold(
        hold_id: int, new: NewTransaction, journal: JournalDep, response: Response
    ) -> Transaction:
        return _respond_posted(journal.capture_hold(hold_id, new), response)

    @app.post(
        "/payments",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "A retry: the original Payment", "model": Payment}},
    )
    def create_payment(new: NewPayment, payments: PaymentsDep, response: Response) -> Payment:
        if new.direction != "pull":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "only pulls are made here: Payouts are made by an Auction Close",
            )
        created = payments.originate(new)
        return _respond(
            created.replayed,
            created.payment,
            response,
            location=f"/payments/{created.payment.id}",
        )

    @app.get("/payments/{payment_id}")
    def get_payment(payment_id: int, payments: PaymentsDep) -> Payment:
        return payments.payment(payment_id)

    @app.get("/return-exceptions")
    def list_return_exceptions(
        payments: PaymentsDep, customer_account: Annotated[str | None, Query()] = None
    ) -> list[ReturnException]:
        return payments.return_exceptions(customer_account)

    @app.post(
        "/auction-closes",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "A retry: the original", "model": AuctionClose}},
    )
    def close_auction(
        new: NewAuctionClose, marketplace: MarketplaceDep, response: Response
    ) -> AuctionClose:
        recorded = marketplace.close(new)
        return _respond(
            recorded.replayed,
            recorded.auction_close,
            response,
            location=f"/auction-closes/{new.auction_id}",
        )

    @app.get("/auction-closes/{auction_id}")
    def get_auction_close(auction_id: str, marketplace: MarketplaceDep) -> AuctionClose:
        return marketplace.auction_close(auction_id)

    @app.get("/settlement-reports/{provider}/{report_date}")
    def get_settlement_reports(
        provider: str, report_date: date, request: Request
    ) -> list[SettlementReportView]:
        found: SettlementReports = request.app.state.reports
        return found.reports(provider, report_date)

    @app.post("/providers/{provider}/events", responses={401: {}, 400: {}, 503: {}})
    async def receive_event(provider: str, request: Request, payments: PaymentsDep) -> Receipt:
        body = await request.body()
        try:
            return await run_in_threadpool(payments.receive, provider, body, dict(request.headers))
        except BadSignature as error:
            log.warning("refused an unsigned webhook", extra={"provider": provider})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
        except MalformedEvent as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error

    return app


def _respond[T](replayed: bool, body: T, response: Response, location: str = "") -> T:
    """A retry answers 200 with `Idempotent-Replayed: true`; a first write keeps its 201,
    with a Location when the new thing has its own URL."""
    if replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    elif location:
        response.headers["Location"] = location
    return body


def _respond_posted(posted: Posted, response: Response) -> Transaction:
    transaction = posted.transaction
    return _respond(
        posted.replayed, transaction, response, location=f"/transactions/{transaction.id}"
    )
