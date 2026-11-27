"""The Ledger API."""

from fastapi import FastAPI
from sqlalchemy import Engine

from railbench.common.clock import Clock
from railbench.common.service import create_service_app


def create_app(engine: Engine, clock: Clock) -> FastAPI:
    return create_service_app("ledger", engine, clock)
