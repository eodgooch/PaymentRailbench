"""What every Railbench HTTP service shares: its database Engine, its Clock and /health."""

from fastapi import FastAPI
from sqlalchemy import Engine

from railbench.common.clock import Clock
from railbench.common.health import health_router


def create_service_app(service: str, engine: Engine, clock: Clock) -> FastAPI:
    app = FastAPI(title=f"Railbench {service}")
    app.state.engine = engine
    app.state.clock = clock
    app.include_router(health_router(service))
    return app
