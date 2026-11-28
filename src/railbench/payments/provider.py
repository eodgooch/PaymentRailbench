"""The Provider interface: the only way the Ledger talks to a Provider (ADR 0005).

A Provider creates and reads Payments, and turns its webhooks into Provider Events: it
verifies a webhook against its signature (`verify_event`), and reads an event's ID, Payment
and reported state out of a body it has already verified (`read_event`), so replaying the
Inbox reads stored bodies without re-verifying them.

`SimulatorProvider` is the HTTP adapter for the Simulator. A real Provider would be another
adapter, not a Ledger change.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from railbench.common.signing import SIGNATURE_HEADER, BadSignature, verify
from railbench.payments.state_machine import AchState


@dataclass(frozen=True)
class PaymentOrder:
    """What the Ledger asks a Provider to do. The Idempotency Key makes a retry safe."""

    idempotency_key: str
    rail: str
    direction: str
    amount: int
    currency: str
    reference: str


@dataclass(frozen=True)
class ProviderPayment:
    provider_payment_id: str
    status: str


@dataclass(frozen=True)
class ProviderEvent:
    event_id: str
    provider_payment_id: str
    reported: AchState
    occurred_at: datetime


class ProviderError(Exception):
    """The Provider refused or failed a request."""


class MalformedEvent(ValueError):
    """A verified webhook whose body isn't a Provider Event this adapter understands."""


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


EVENT_STATES = {
    "payment.submitted": AchState.SUBMITTED,
    "payment.settled": AchState.SETTLED,
    "payment.canceled": AchState.CANCELED,
    "payment.declined": AchState.DECLINED,
}


class SimulatorProvider:
    """The Simulator, over its HTTP API and signed webhooks."""

    def __init__(
        self,
        base_url: str,
        signing_secret: str,
        name: str = "simulator",
        http: httpx.Client | None = None,
    ) -> None:
        self.name = name
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
        signature = next(
            (value for key, value in headers.items() if key.lower() == SIGNATURE_HEADER.lower()),
            None,
        )
        if signature is None:
            raise BadSignature(f"no {SIGNATURE_HEADER} header")
        verify(self._secret, body, signature, now)

    def read_event(self, body: bytes) -> ProviderEvent:
        try:
            event = json.loads(body)
            occurred_at = datetime.fromisoformat(event["occurred_at"])
            if occurred_at.utcoffset() is None:
                raise ValueError("occurred_at has no UTC offset")
            return ProviderEvent(
                event_id=str(event["event_id"]),
                provider_payment_id=str(event["payment"]["id"]),
                reported=EVENT_STATES[event["type"]],
                occurred_at=occurred_at,
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
        return ProviderPayment(provider_payment_id=body["id"], status=body["status"])
