"""Signed Provider Events: HMAC-SHA256 over the timestamp and the raw body, with a bounded
timestamp so a captured webhook can't be replayed later (ADR 0010)."""

from datetime import UTC, datetime, timedelta

import pytest

from railbench.common.signing import TOLERANCE, BadSignature, sign, verify

SECRET = "test-secret"
SENT = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)
BODY = b'{"event_id":"evt_1","type":"settled"}'
HEADER = "t=1788525000,v1=860a916aa7d310013f534334019f7bcc2363882d64a5773aa70e220cbb625555"


def test_a_known_signature() -> None:
    # Computed independently:
    # printf '1788525000.<body>' | openssl dgst -sha256 -hmac test-secret
    assert sign(SECRET, BODY, SENT) == HEADER


def test_a_signed_body_verifies_within_the_tolerance() -> None:
    verify(SECRET, BODY, HEADER, now=SENT + TOLERANCE)
    verify(SECRET, BODY, HEADER, now=SENT - TOLERANCE)


@pytest.mark.parametrize(
    ("body", "header", "secret"),
    [
        pytest.param(BODY + b" ", HEADER, SECRET, id="a changed body"),
        pytest.param(BODY, HEADER.replace("t=1788525000", "t=1788525001"), SECRET, id="new time"),
        pytest.param(BODY, HEADER[:-1] + "4", SECRET, id="a changed signature"),
        pytest.param(BODY, HEADER, "another-secret", id="another secret"),
        pytest.param(BODY, HEADER.split(",")[1], SECRET, id="no timestamp"),
        pytest.param(BODY, HEADER.split(",")[0], SECRET, id="no signature"),
        pytest.param(BODY, "t=soon,v1=abc", SECRET, id="a malformed timestamp"),
        pytest.param(BODY, "", SECRET, id="no header"),
    ],
)
def test_a_tampered_event_is_refused(body: bytes, header: str, secret: str) -> None:
    with pytest.raises(BadSignature):
        verify(secret, body, header, now=SENT)


def test_an_old_or_future_signature_is_refused() -> None:
    late = SENT + TOLERANCE + timedelta(seconds=1)
    early = SENT - TOLERANCE - timedelta(seconds=1)
    for now in (late, early):
        with pytest.raises(BadSignature, match="tolerance"):
            verify(SECRET, BODY, HEADER, now=now)
