"""What every Railbench HTTP service shares: its database Engine, its Clock and /health."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI
from sqlalchemy import Engine

from railbench.common.clock import Clock
from railbench.common.health import health_router

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_service_app(
    service: str, engine: Engine, clock: Clock, lifespan: Lifespan | None = None
) -> FastAPI:
    app = FastAPI(title=f"Railbench {service}", lifespan=lifespan)
    app.state.engine = engine
    app.state.clock = clock
    app.include_router(health_router(service))
    return app
