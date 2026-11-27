"""The Simulator: a separate service that plays every Provider (ADR 0005)."""

from fastapi import FastAPI
from sqlalchemy import Engine

from railbench.common.clock import Clock
from railbench.common.service import create_service_app


def create_app(engine: Engine, clock: Clock) -> FastAPI:
    return create_service_app("simulator", engine, clock)
