"""Payload fingerprints for Idempotency Keys: tell a retry from a different request that
reuses its key.

A SHA-256 of the payload's canonical JSON: keys sorted, no whitespace, times as UTC
instants (16:00-05:00 and 21:00Z are the same payload). The Ledger stores these, so the
output must never change for a given payload (a test pins it).
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_instant)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _instant(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"can't fingerprint {type(value).__name__}")
