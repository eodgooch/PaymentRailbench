"""Webhook signatures for Provider Events (ADR 0010).

The sender signs `"<unix seconds>.<raw body>"` with HMAC-SHA256 under the shared
WEBHOOK_SIGNING_SECRET and sends `Railbench-Signature: t=<unix seconds>,v1=<hex digest>`.
The receiver recomputes the digest over the exact bytes it received, compares in constant
time, and refuses a timestamp more than TOLERANCE from its own Clock, so a captured webhook
can't be replayed later. The sender signs each delivery attempt afresh.

This module is the wire contract both sides share, the way a Provider's SDK would be; it
imports nothing from either service.
"""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

SIGNATURE_HEADER = "Railbench-Signature"
TOLERANCE = timedelta(minutes=5)


class BadSignature(Exception):
    """The webhook is not signed by the Provider, or not recently."""


def _digest(secret: str, body: bytes, timestamp: int) -> str:
    message = str(timestamp).encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, at: datetime) -> str:
    """The signature header value for `body`, sent at `at`."""
    timestamp = int(at.astimezone(UTC).timestamp())
    return f"t={timestamp},v1={_digest(secret, body, timestamp)}"


def verify(secret: str, body: bytes, header: str, now: datetime) -> None:
    """Raise BadSignature unless `header` signs `body` under `secret` within TOLERANCE of
    `now`."""
    parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    timestamp, signature = parts.get("t", ""), parts.get("v1", "")
    if not timestamp.isdigit() or not signature:
        raise BadSignature("missing or malformed signature header")
    expected = _digest(secret, body, int(timestamp))
    if not hmac.compare_digest(expected, signature):
        raise BadSignature("signature does not match the body")
    sent = datetime.fromtimestamp(int(timestamp), UTC)
    if abs(now - sent) > TOLERANCE:
        raise BadSignature("signature timestamp is outside the tolerance")
