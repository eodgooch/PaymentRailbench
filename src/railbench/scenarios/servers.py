"""Run an ASGI app on a real localhost port in a background thread, so the Simulator and the
Ledger talk over real HTTP in tests and Scenario runs (ADR 0005) while sharing one
ManualClock in-process."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI


def free_port() -> int:
    """A port nothing listens on right now, so two apps can know each other's URL before
    either starts."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextmanager
def serving(app: FastAPI, port: int = 0) -> Iterator[str]:
    """Serve `app` on 127.0.0.1 at `port` (0: any free port); yields its base URL."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        time.sleep(0.01)
    else:
        raise RuntimeError("the test server didn't start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
