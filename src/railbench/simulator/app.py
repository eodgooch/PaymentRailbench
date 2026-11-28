"""The Simulator: a separate service that plays every Provider (ADR 0005).

`POST /payments` creates a Payment once per Idempotency Key: 201 for a new one, 200 with
`Idempotent-Replayed: true` for a retry of the same request, 409 for the same key with a
different payload (or a plan the Payment can't follow), 422 for a planned Return that would
fall outside its Return Window. `GET /payments/{id}` reads one. `POST /payments/{id}/returns`
returns a settled ACH pull now with a NACHA return code (201; 409 if it can't be returned,
422 outside the code's Return Window or for an unknown code): a control endpoint for tests
and demos, as a Provider's sandbox would have. Provider Events go out as signed
webhooks to LEDGER_WEBHOOK_URL whenever `Simulator.tick()` runs: every
SIMULATOR_TICK_SECONDS in a running service, by hand in tests.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response, status
from sqlalchemy import Engine

from railbench.common.clock import Clock
from railbench.common.service import create_service_app
from railbench.common.settings import Settings
from railbench.common.ticker import ticking
from railbench.simulator.model import PaymentRequest, ReturnRequest, SimulatedPayment
from railbench.simulator.provider import (
    IdempotencyConflict,
    PaymentNotFound,
    PlanMismatch,
    ReturnOutsideWindow,
    ReturnRefused,
    Simulator,
)


def get_simulator(request: Request) -> Simulator:
    simulator: Simulator = request.app.state.simulator
    return simulator


def create_app(
    engine: Engine,
    clock: Clock,
    settings: Settings | None = None,
    simulator: Simulator | None = None,
) -> FastAPI:
    """The Simulator's API. `simulator` defaults to one built from `settings`."""
    settings = settings or Settings()
    simulator = simulator or Simulator(
        engine,
        clock,
        webhook_url=settings.ledger_webhook_url,
        signing_secret=settings.webhook_signing_secret.get_secret_value(),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        with ticking("simulator", simulator.tick, settings.simulator_tick_seconds):
            yield

    app = create_service_app("simulator", engine, clock, lifespan=lifespan)
    app.state.simulator = simulator

    @app.post(
        "/payments",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "A retry: the original Payment"}, 409: {}},
    )
    def create_payment(payment: PaymentRequest, response: Response) -> SimulatedPayment:
        try:
            created, replayed = simulator.create_payment(payment)
        except (IdempotencyConflict, PlanMismatch) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        except ReturnOutsideWindow as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        if replayed:
            response.status_code = status.HTTP_200_OK
            response.headers["Idempotent-Replayed"] = "true"
        else:
            response.headers["Location"] = f"/payments/{created.id}"
        return created

    @app.get("/payments/{payment_id}")
    def get_payment(payment_id: str) -> SimulatedPayment:
        try:
            return simulator.payment(payment_id)
        except PaymentNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no Payment {payment_id}") from error

    @app.post(
        "/payments/{payment_id}/returns",
        status_code=status.HTTP_201_CREATED,
        responses={404: {}, 409: {}, 422: {}},
    )
    def return_payment(payment_id: str, request: ReturnRequest) -> SimulatedPayment:
        try:
            return simulator.return_payment(payment_id, request.code)
        except PaymentNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no Payment {payment_id}") from error
        except ReturnRefused as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        except ReturnOutsideWindow as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error

    return app
