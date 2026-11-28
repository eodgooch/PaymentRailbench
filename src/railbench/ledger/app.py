"""The Ledger API: a thin HTTP layer over the Journal.

Posting is idempotent. The first post of an Idempotency Key answers 201 Created with a
Location; a retry with the same payload answers 200 OK with the original Transaction and
`Idempotent-Replayed: true`; the same key with a different payload answers 409 Conflict.
Reversals, Settlements, Holds and their releases and captures work the same way; a second
Reversal, Settlement, release or capture of the same thing is a 409.

Balances are read at an Effective Time, as known at a Recorded Time (default now): both are
query parameters and must carry a UTC offset.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy import Engine

from railbench.common.clock import Clock
from railbench.common.service import create_service_app
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

ERROR_STATUS = {
    NotFound: status.HTTP_404_NOT_FOUND,
    Conflict: status.HTTP_409_CONFLICT,
    Rejected: status.HTTP_422_UNPROCESSABLE_CONTENT,
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


def create_app(engine: Engine, clock: Clock) -> FastAPI:
    app = create_service_app("ledger", engine, clock)
    app.state.journal = Journal(engine, clock)

    @app.exception_handler(LedgerError)
    def ledger_error(_request: Request, error: LedgerError) -> JSONResponse:
        return JSONResponse(status_code=error_status(error), content={"detail": str(error)})

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
