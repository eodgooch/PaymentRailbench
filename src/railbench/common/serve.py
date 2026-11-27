"""Run a Railbench service under uvicorn with settings from the environment."""

from collections.abc import Callable

import uvicorn
from fastapi import FastAPI
from sqlalchemy import Engine

from railbench.common.clock import Clock, WallClock
from railbench.common.db import make_engine
from railbench.common.logging import configure_logging
from railbench.common.settings import Settings


def serve(
    service: str,
    create_app: Callable[[Engine, Clock], FastAPI],
    port: Callable[[Settings], int],
    database_role: Callable[[Settings], str | None] = lambda _settings: None,
) -> None:
    settings = Settings()
    configure_logging(service, settings.log_level)
    engine = make_engine(settings.database_url, role=database_role(settings))
    app = create_app(engine, WallClock())
    uvicorn.run(app, host="0.0.0.0", port=port(settings), log_config=None)
