"""The Account codes and Ledger Idempotency Keys Payments books under (SPEC §3.1)."""

import re


def clearing_account(provider: str, rail: str, currency: str) -> str:
    """The Clearing Account for one Provider, Rail and currency (SPEC §3.1)."""
    return f"clearing:{provider}:{rail}:{currency.lower()}"


def customer_balance_account(customer_id: str, currency: str) -> str:
    """The Customer Balance of one Customer in one currency (SPEC §3.1)."""
    return f"customer:{customer_id}:{currency.lower()}"


_LEDGER_KEY = re.compile(r"^payment:(\d+):(.+)$")


def ledger_key(payment_id: int, step: str) -> str:
    """The Idempotency Key of the Ledger row a Payment's step books, e.g. its pending
    Transaction or its availability Hold."""
    return f"payment:{payment_id}:{step}"


def parse_ledger_key(key: str) -> tuple[int, str] | None:
    """The Payment id and step in a `ledger_key`, or None for any other key."""
    match = _LEDGER_KEY.match(key)
    return None if match is None else (int(match.group(1)), match.group(2))


def recovery_receivable_account(customer_account: str) -> str:
    """The Recovery Receivable for one Customer Balance (one Customer, one currency): what
    that Customer owes Mintwharf after a Return left their balance short (SPEC §3.1)."""
    return f"recovery-receivable:{customer_account}"


def bank_cash_account(currency: str) -> str:
    return f"bank-cash:{currency.lower()}"
