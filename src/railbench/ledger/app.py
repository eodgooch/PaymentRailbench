"""The Ledger API: a thin HTTP layer over the Journal.

Posting is idempotent. The first post of an Idempotency Key answers 201 Created with a
Location; a retry with the same payload answers 200 OK with the original Transaction and
`Idempotent-Replayed: true`; the same key with a different payload answers 409 Conflict.
Reversals work the same way, and a second Reversal of one Transaction is a 409.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from railbench.common.clock import Clock
from railbench.common.service import create_service_app
from railbench.ledger.journal import Conflict, Journal, LedgerError, NotFound, Rejected
from railbench.ledger.model import (
    Account,
    NewAccount,
    NewTransaction,
    Posted,
    ReversalRequest,
    Transaction,
)

ERROR_STATUS = {
    NotFound: status.HTTP_404_NOT_FOUND,
    Conflict: status.HTTP_409_CONFLICT,
    Rejected: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


class AccountWithBalance(Account):
    posted_balance: int


def get_journal(request: Request) -> Journal:
    journal: Journal = request.app.state.journal
    return journal


JournalDep = Annotated[Journal, Depends(get_journal)]


def create_app(engine: Engine, clock: Clock) -> FastAPI:
    app = create_service_app("ledger", engine, clock)
    app.state.journal = Journal(engine, clock)

    @app.exception_handler(LedgerError)
    def ledger_error(_request: Request, error: LedgerError) -> JSONResponse:
        code = next(code for kind, code in ERROR_STATUS.items() if isinstance(error, kind))
        return JSONResponse(status_code=code, content={"detail": str(error)})

    @app.post("/accounts", status_code=status.HTTP_201_CREATED)
    def open_account(new: NewAccount, journal: JournalDep) -> Account:
        return journal.open_account(new)

    @app.get("/accounts/{code}")
    def get_account(code: str, journal: JournalDep) -> AccountWithBalance:
        account = journal.account(code)
        return AccountWithBalance(
            **account.model_dump(), posted_balance=journal.posted_balance(code)
        )

    @app.post(
        "/transactions",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "A retry: the original Transaction", "model": Transaction}},
    )
    def post_transaction(
        new: NewTransaction, journal: JournalDep, response: Response
    ) -> Transaction:
        return _respond(journal.post(new), response)

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
        return _respond(journal.reverse(transaction_id, request), response)

    return app


def _respond(posted: Posted, response: Response) -> Transaction:
    if posted.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    else:
        response.headers["Location"] = f"/transactions/{posted.transaction.id}"
    return posted.transaction
