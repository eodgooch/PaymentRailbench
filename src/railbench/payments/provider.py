"""The Provider interface: the only way the Ledger talks to a Provider (ADR 0005).

A Provider creates and reads Payments, and turns its webhooks into Provider Events: it
verifies a webhook against its signature (`verify_event`), and reads an event's ID, Payment
and reported state out of a body it has already verified (`read_event`), so replaying the
Inbox reads stored bodies without re-verifying them.

It also serves one Settlement Report per Banking Day (ADR 0014): `fetch_settlement_report`
fetches one (None if it isn't published), `verify_report` checks it's signed by the
Provider, recently, and `read_settlement_report` reads its lines out of a body, strictly:
anything off (a column, an amount that isn't an integer, a time without an offset, a line
for another day) is a MalformedReport, never a guess.

`SimulatorProvider` is the HTTP adapter for the Simulator. A real Provider would be another
adapter, not a Ledger change.
"""

import csv
import io
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, Protocol, cast, get_args

import httpx

from railbench.common.returns import ReturnCode, return_code
from railbench.common.settlement_reports import COLUMNS, AdjustmentReason
from railbench.common.signing import SIGNATURE_HEADER, BadSignature, verify
from railbench.payments.state_machine import Direction, PaymentState, Rail


@dataclass(frozen=True)
class PaymentOrder:
    """What the Ledger asks a Provider to do. The Idempotency Key makes a retry safe."""

    idempotency_key: str
    rail: Rail
    direction: Direction
    amount: int
    currency: str
    reference: str


@dataclass(frozen=True)
class ProviderPayment:
    provider_payment_id: str
    status: PaymentState


@dataclass(frozen=True)
class ProviderEvent:
    event_id: str
    provider_payment_id: str
    reported: PaymentState
    occurred_at: datetime
    # A Return's NACHA return code (`returned` events only).
    return_code: ReturnCode | None = None


LineType = Literal["settlement", "adjustment"]


@dataclass(frozen=True)
class ReportLine:
    """One line of a Settlement Report, as the Provider stated it. `amount` is signed: money
    into (+) or out of (-) Mintwharf's account at the Provider. An adjustment line names the
    settlement line it adjusts and why."""

    line_id: str
    type: LineType
    provider_payment_id: str
    idempotency_key: str
    rail: Rail
    direction: Direction
    currency: str
    amount: int
    settled_at: datetime
    original_line_id: str | None = None
    reason: AdjustmentReason | None = None


@dataclass(frozen=True)
class SettlementReport:
    provider: str
    report_date: date
    lines: tuple[ReportLine, ...]


@dataclass(frozen=True)
class FetchedReport:
    """A Settlement Report as the Provider served it: the raw body and response headers."""

    body: bytes
    headers: dict[str, str]


class ProviderError(Exception):
    """The Provider refused or failed a request."""


class MalformedEvent(ValueError):
    """A verified webhook whose body isn't a Provider Event this adapter understands."""


class MalformedReport(ValueError):
    """A body that isn't a Settlement Report this adapter can read, for the day asked."""


class Provider(Protocol):
    name: str

    def create_payment(self, order: PaymentOrder) -> ProviderPayment: ...

    def get_payment(self, provider_payment_id: str) -> ProviderPayment: ...

    def verify_event(self, body: bytes, headers: Mapping[str, str], now: datetime) -> None:
        """Raise BadSignature unless the webhook is signed by this Provider, recently."""
        ...

    def read_event(self, body: bytes) -> ProviderEvent:
        """The Provider Event in a verified body. Raises MalformedEvent."""
        ...

    def fetch_settlement_report(self, report_date: date) -> FetchedReport | None:
        """The Provider's Settlement Report for `report_date`, None if not published."""
        ...

    def verify_report(self, body: bytes, headers: Mapping[str, str], now: datetime) -> None:
        """Raise BadSignature unless the report is signed by this Provider, recently."""
        ...

    def read_settlement_report(self, report_date: date, body: bytes) -> SettlementReport:
        """The Settlement Report for `report_date` in a body. Raises MalformedReport."""
        ...


EVENT_STATES = {
    "payment.submitted": PaymentState.SUBMITTED,
    "payment.settled": PaymentState.SETTLED,
    "payment.canceled": PaymentState.CANCELED,
    "payment.declined": PaymentState.DECLINED,
    "payment.returned": PaymentState.RETURNED,
}


class SimulatorProvider:
    """The Simulator, over its HTTP API and signed webhooks."""

    name = "simulator"

    def __init__(
        self, base_url: str, signing_secret: str, http: httpx.Client | None = None
    ) -> None:
        self._secret = signing_secret
        self._http = http or httpx.Client(base_url=base_url, timeout=10.0)

    def create_payment(self, order: PaymentOrder) -> ProviderPayment:
        return self._payment(
            self._request(
                "POST",
                "/payments",
                json={
                    "idempotency_key": order.idempotency_key,
                    "rail": order.rail,
                    "direction": order.direction,
                    "amount": order.amount,
                    "currency": order.currency,
                    "reference": order.reference,
                },
            )
        )

    def get_payment(self, provider_payment_id: str) -> ProviderPayment:
        return self._payment(self._request("GET", f"/payments/{provider_payment_id}"))

    def verify_event(self, body: bytes, headers: Mapping[str, str], now: datetime) -> None:
        _verify(self._secret, body, headers, now)

    def fetch_settlement_report(self, report_date: date) -> FetchedReport | None:
        path = f"/settlement-reports/{self.name}/{report_date.isoformat()}"
        try:
            response = self._http.get(path)
        except httpx.HTTPError as error:
            raise ProviderError(f"{self.name} unreachable: {error}") from error
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise ProviderError(f"{self.name} answered {response.status_code}: {response.text}")
        return FetchedReport(body=response.content, headers=dict(response.headers))

    def verify_report(self, body: bytes, headers: Mapping[str, str], now: datetime) -> None:
        _verify(self._secret, body, headers, now)

    def read_settlement_report(self, report_date: date, body: bytes) -> SettlementReport:
        return read_report_csv(self.name, report_date, body)

    def read_event(self, body: bytes) -> ProviderEvent:
        try:
            event = json.loads(body)
            occurred_at = datetime.fromisoformat(event["occurred_at"])
            if occurred_at.utcoffset() is None:
                raise ValueError("occurred_at has no UTC offset")
            reported = EVENT_STATES[event["type"]]
            code = None
            if reported is PaymentState.RETURNED:  # an unknown code is a ValueError too
                code = return_code(str(event["return"]["code"]))
            return ProviderEvent(
                event_id=str(event["event_id"]),
                provider_payment_id=str(event["payment"]["id"]),
                reported=reported,
                occurred_at=occurred_at,
                return_code=code,
            )
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise MalformedEvent(f"not a Simulator Provider Event: {error!r}") from error

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise ProviderError(f"{self.name} unreachable: {error}") from error
        if not response.is_success:
            raise ProviderError(f"{self.name} answered {response.status_code}: {response.text}")
        body: dict[str, Any] = response.json()
        return body

    @staticmethod
    def _payment(body: dict[str, Any]) -> ProviderPayment:
        return ProviderPayment(provider_payment_id=body["id"], status=PaymentState(body["status"]))


def _verify(secret: str, body: bytes, headers: Mapping[str, str], now: datetime) -> None:
    signature = next(
        (value for key, value in headers.items() if key.lower() == SIGNATURE_HEADER.lower()),
        None,
    )
    if signature is None:
        raise BadSignature(f"no {SIGNATURE_HEADER} header")
    verify(secret, body, signature, now)


_AMOUNT = re.compile(r"^-?[1-9][0-9]{0,18}$")
_CURRENCY = re.compile(r"^[A-Z]{3,5}$")


def read_report_csv(provider: str, report_date: date, body: bytes) -> SettlementReport:
    """Read a Settlement Report in the shared CSV format (`railbench.common
    .settlement_reports`) for `provider` and `report_date`. Raises MalformedReport."""
    try:
        rows = list(csv.reader(io.StringIO(body.decode("utf-8"), newline="")))
    except (UnicodeDecodeError, csv.Error) as error:
        raise MalformedReport(f"not a CSV Settlement Report: {error}") from error
    if not rows or tuple(rows[0]) != COLUMNS:
        raise MalformedReport(f"the header isn't {','.join(COLUMNS)}")
    lines: list[ReportLine] = []
    for number, row in enumerate(rows[1:], start=2):
        try:
            lines.append(_line(provider, report_date, dict(zip(COLUMNS, row, strict=True))))
        except (ValueError, KeyError) as error:
            raise MalformedReport(f"row {number}: {error}") from error
    ids = [line.line_id for line in lines]
    if len(set(ids)) != len(ids):
        raise MalformedReport("a line ID appears twice")
    return SettlementReport(provider=provider, report_date=report_date, lines=tuple(lines))


def _line(provider: str, report_date: date, row: dict[str, str]) -> ReportLine:
    if row["report_date"] != report_date.isoformat() or row["provider"] != provider:
        raise ValueError(f"a line for {row['provider']} {row['report_date']}")
    if row["type"] not in get_args(LineType):
        raise ValueError(f"unknown line type {row['type']!r}")
    if row["rail"] not in get_args(Rail) or row["direction"] not in get_args(Direction):
        raise ValueError(f"unknown Rail or direction {row['rail']!r} {row['direction']!r}")
    if not _AMOUNT.match(row["amount"]) or not _CURRENCY.match(row["currency"]):
        raise ValueError(f"not an amount in Minor Units: {row['amount']!r} {row['currency']!r}")
    settled_at = datetime.fromisoformat(row["settled_at"])
    if settled_at.utcoffset() is None:
        raise ValueError("settled_at has no UTC offset")
    for name in ("line_id", "provider_payment_id"):
        if not row[name]:
            raise ValueError(f"no {name}")
    adjustment = row["type"] == "adjustment"
    if adjustment != bool(row["original_line_id"]) or adjustment != bool(row["reason"]):
        raise ValueError("only an adjustment line, and every one, names its original and reason")
    if adjustment and row["reason"] not in set(AdjustmentReason):
        raise ValueError(f"unknown adjustment reason {row['reason']!r}")
    return ReportLine(
        line_id=row["line_id"],
        type=cast(LineType, row["type"]),
        provider_payment_id=row["provider_payment_id"],
        idempotency_key=row["idempotency_key"],
        rail=cast(Rail, row["rail"]),
        direction=cast(Direction, row["direction"]),
        currency=row["currency"],
        amount=int(row["amount"]),
        settled_at=settled_at,
        original_line_id=row["original_line_id"] or None,
        reason=AdjustmentReason(row["reason"]) if adjustment else None,
    )
