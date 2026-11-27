"""How the Ledger API answers each kind of refusal."""

import pytest

from railbench.ledger.app import error_status
from railbench.ledger.journal import (
    AccountNotFound,
    AlreadyReversed,
    CannotReverseReversal,
    CurrencyMismatch,
    IdempotencyConflict,
    LedgerError,
    TransactionNotFound,
    UnknownAccount,
)


class UnmappedRefusal(LedgerError):
    """A refusal added later without its own HTTP status."""


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AccountNotFound(), 404),
        (TransactionNotFound(), 404),
        (IdempotencyConflict(), 409),
        (AlreadyReversed(), 409),
        (CannotReverseReversal(), 409),
        (UnknownAccount(), 422),
        (CurrencyMismatch(), 422),
        (UnmappedRefusal(), 422),
        (LedgerError(), 422),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else str(value),
)
def test_each_refusal_has_an_http_status(error: LedgerError, expected: int) -> None:
    assert error_status(error) == expected
