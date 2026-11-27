"""`GET /health`: is the service up, and can it reach its database?"""

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from railbench.common.clock import Clock

log = logging.getLogger(__name__)


class Health(BaseModel):
    service: str
    status: Literal["ok", "unavailable"]
    database: Literal["ok", "unreachable"]
    checked_at: datetime


def get_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def database_is_reachable(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        log.warning("database health check failed", exc_info=True)
        return False
    return True


def health_router(service: str) -> APIRouter:
    router = APIRouter()

    @router.get("/health", response_model=Health)
    def health(
        response: Response,
        engine: Annotated[Engine, Depends(get_engine)],
        clock: Annotated[Clock, Depends(get_clock)],
    ) -> Health:
        if database_is_reachable(engine):
            return Health(service=service, status="ok", database="ok", checked_at=clock.now())
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(
            service=service,
            status="unavailable",
            database="unreachable",
            checked_at=clock.now(),
        )

    return router
