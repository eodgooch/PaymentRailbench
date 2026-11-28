"""A background loop that runs a service's Clock-driven work every few seconds.

Only running services use it (`SIMULATOR_TICK_SECONDS`, `LEDGER_TICK_SECONDS`); tests call
the same `tick()` by hand on a ManualClock, so nothing in a test waits on real time. The
loop waits on an Event rather than reading the system time.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)


@contextmanager
def ticking(name: str, work: Callable[[], object], seconds: float) -> Iterator[None]:
    """Run `work` every `seconds` in a daemon thread until the block exits. A failure is
    logged and the loop carries on. `seconds` <= 0 runs nothing."""
    if seconds <= 0:
        yield
        return
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(seconds):
            try:
                work()
            except Exception:
                log.exception("tick failed", extra={"ticker": name})

    thread = threading.Thread(target=loop, name=f"{name}-ticker", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=seconds + 5)
