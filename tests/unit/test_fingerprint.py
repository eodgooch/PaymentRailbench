"""The payload fingerprint behind every Idempotency Key. Stored fingerprints must keep
matching, so its output is pinned."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from railbench.common.fingerprint import fingerprint

PAYLOAD = {
    "entries": [{"amount": -1250, "account": "customer:c-1"}],
    "amount": 1250,
    "effective_at": datetime(2026, 3, 2, 21, 0, tzinfo=UTC),
}
# Computed independently:
# printf '%s' '{"amount":1250,"effective_at":"2026-03-02T21:00:00+00:00",
#   "entries":[{"account":"customer:c-1","amount":-1250}]}' | sha256sum
PINNED = "cbb1a309f851b092339fe4cbc51c5acddc81fe72c236812575ad2aa8ab25bfb4"


def test_the_fingerprint_is_pinned() -> None:
    assert fingerprint(PAYLOAD) == PINNED


def test_times_compare_as_instants() -> None:
    eastern = datetime(2026, 3, 2, 16, 0, tzinfo=timezone(timedelta(hours=-5)))

    assert fingerprint({**PAYLOAD, "effective_at": eastern}) == PINNED


def test_a_value_it_cannot_canonicalise_is_refused() -> None:
    with pytest.raises(TypeError):
        fingerprint({"amount": object()})
