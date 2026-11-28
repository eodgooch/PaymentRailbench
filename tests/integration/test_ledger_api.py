"""The Ledger API over HTTP: a thin layer on the Journal, so these cover status codes and the
wire format; the Journal's own tests cover the rules."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from railbench.common.clock import ManualClock
from railbench.ledger.app import create_app
from tests.integration.helpers import unique

pytestmark = pytest.mark.integration

AUCTION_CLOSE = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)
SETTLEMENT_JOB = {"kind": "system_job", "name": "ach-settlement", "version": "1.0.0"}


@pytest.fixture
def client(app_engine: Engine) -> TestClient:
    return TestClient(create_app(app_engine, ManualClock(AUCTION_CLOSE)))


def open_account(client: TestClient, type_: str, currency: str = "USD") -> str:
    code = unique(type_)
    response = client.post("/accounts", json={"code": code, "type": type_, "currency": currency})
    assert response.status_code == 201, response.text
    return code


def funding(debit: str, credit: str, amount: int = 3_000, key: str = "") -> dict[str, Any]:
    return {
        "idempotency_key": key or unique("funding"),
        "actor": SETTLEMENT_JOB,
        "effective_at": "2026-03-02T21:00:00Z",
        "description": "ACH funding settled",
        "entries": [
            {"account": debit, "currency": "USD", "amount": amount},
            {"account": credit, "currency": "USD", "amount": -amount},
        ],
    }


def test_an_account_reports_its_type_and_currency(client: TestClient) -> None:
    customer = open_account(client, "liability")

    response = client.get(f"/accounts/{customer}")

    assert response.status_code == 200
    assert response.json() == {
        "code": customer,
        "type": "liability",
        "currency": "USD",
        "recorded_at": "2026-03-02T21:00:00Z",
    }


def balance(client: TestClient, code: str, **params: str) -> dict[str, Any]:
    response = client.get(
        f"/accounts/{code}/balances", params={"effective_at": "2026-03-03T21:00:00Z", **params}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_an_account_reports_its_derived_balances(client: TestClient) -> None:
    clearing, customer = open_account(client, "asset"), open_account(client, "liability")
    client.post("/transactions", json=funding(clearing, customer, 3_000))

    assert balance(client, customer) == {
        "account": customer,
        "currency": "USD",
        "effective_at": "2026-03-03T21:00:00Z",
        "known_at": "2026-03-02T21:00:00Z",
        "posted": 3_000,
        "pending": 0,
        "held": 0,
        "unsettled_outflows": 0,
        "available": 3_000,
    }
    assert balance(client, customer, effective_at="2026-03-02T15:59:59-05:00")["posted"] == 0
    assert balance(client, customer, known_at="2026-03-02T20:59:59Z")["posted"] == 0


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({}, id="no effective time"),
        pytest.param({"effective_at": "2026-03-02T21:00:00"}, id="no offset"),
        pytest.param(
            {"effective_at": "2026-03-02T21:00:00Z", "known_at": "2026-03-02T21:00:00"},
            id="known_at without an offset",
        ),
        pytest.param(
            {"effective_at": "2026-03-02T21:00:00Z", "known_at": "2026-03-03T21:00:00Z"},
            id="known_at after now",
        ),
    ],
)
def test_a_balance_needs_aware_times_no_later_than_now(
    client: TestClient, params: dict[str, str]
) -> None:
    customer = open_account(client, "liability")

    assert client.get(f"/accounts/{customer}/balances", params=params).status_code == 422


def test_the_balance_of_an_unknown_account_is_not_found(client: TestClient) -> None:
    response = client.get(
        "/accounts/nobody/balances", params={"effective_at": "2026-03-02T21:00:00Z"}
    )

    assert response.status_code == 404


def test_an_unknown_account_is_not_found(client: TestClient) -> None:
    assert client.get("/accounts/nobody").status_code == 404


def test_an_account_code_is_taken_once(client: TestClient) -> None:
    code = open_account(client, "revenue")

    response = client.post("/accounts", json={"code": code, "type": "revenue", "currency": "USD"})

    assert response.status_code == 409


def test_posting_creates_a_transaction_and_a_retry_returns_it(client: TestClient) -> None:
    body = funding(open_account(client, "asset"), open_account(client, "liability"))

    created = client.post("/transactions", json=body)
    retried = client.post("/transactions", json=body)

    assert created.status_code == 201
    assert created.headers["Location"] == f"/transactions/{created.json()['id']}"
    assert "Idempotent-Replayed" not in created.headers
    assert retried.status_code == 200
    assert retried.headers["Idempotent-Replayed"] == "true"
    assert retried.json() == created.json()
    assert client.get(f"/transactions/{created.json()['id']}").json() == created.json()


def test_a_transaction_shows_its_entries_and_actor(client: TestClient) -> None:
    clearing, customer = open_account(client, "asset"), open_account(client, "liability")

    body = client.post("/transactions", json=funding(clearing, customer, 3_000)).json()

    assert body["description"] == "ACH funding settled"
    assert body["status"] == "posted"
    assert body["reverses"] is None
    assert body["reversed_by"] is None
    assert body["settlement"] is None
    assert [(e["account"], e["amount"], e["actor"]) for e in body["entries"]] == [
        (clearing, 3_000, SETTLEMENT_JOB),
        (customer, -3_000, SETTLEMENT_JOB),
    ]
    assert body["entries"][0]["effective_at"] == "2026-03-02T21:00:00Z"
    assert body["entries"][0]["recorded_at"] == "2026-03-02T21:00:00Z"


def test_reusing_a_key_with_another_payload_is_a_conflict(client: TestClient) -> None:
    clearing, customer = open_account(client, "asset"), open_account(client, "liability")
    key = unique("funding")
    client.post("/transactions", json=funding(clearing, customer, 3_000, key=key))

    response = client.post("/transactions", json=funding(clearing, customer, 3_001, key=key))

    assert response.status_code == 409
    assert key in response.json()["detail"]


@pytest.mark.parametrize("amount", [30.0, "3000", 30.5, True], ids=repr)
def test_money_is_a_json_integer(client: TestClient, amount: Any) -> None:
    body = funding(open_account(client, "asset"), open_account(client, "liability"))
    body["entries"][0]["amount"] = amount

    assert client.post("/transactions", json=body).status_code == 422


def test_an_unbalanced_transaction_is_unprocessable(client: TestClient) -> None:
    body = funding(open_account(client, "asset"), open_account(client, "liability"))
    body["entries"][1]["amount"] = -2_999

    response = client.post("/transactions", json=body)

    assert response.status_code == 422
    assert "sum to zero" in response.text


def test_an_entry_without_an_actor_is_unprocessable(client: TestClient) -> None:
    body = funding(open_account(client, "asset"), open_account(client, "liability"))
    del body["actor"]

    assert client.post("/transactions", json=body).status_code == 422


def test_an_entry_to_an_unknown_account_is_unprocessable(client: TestClient) -> None:
    response = client.post("/transactions", json=funding(open_account(client, "asset"), "nobody"))

    assert response.status_code == 422
    assert "nobody" in response.json()["detail"]


def test_an_unknown_transaction_is_not_found(client: TestClient) -> None:
    assert client.get(f"/transactions/{2**62}").status_code == 404


def reversal(key: str = "") -> dict[str, Any]:
    return {
        "idempotency_key": key or unique("reversal"),
        "actor": {"kind": "person", "operator_id": "op-17", "proposal_id": "prop-204"},
        "effective_at": "2026-03-03T15:00:00Z",
    }


def test_reversing_a_transaction_once(client: TestClient) -> None:
    clearing, customer = open_account(client, "asset"), open_account(client, "liability")
    original = client.post("/transactions", json=funding(clearing, customer, 3_000)).json()
    body = reversal()

    created = client.post(f"/transactions/{original['id']}/reversal", json=body)
    retried = client.post(f"/transactions/{original['id']}/reversal", json=body)
    again = client.post(f"/transactions/{original['id']}/reversal", json=reversal())

    assert created.status_code == 201
    assert created.json()["reverses"] == original["id"]
    assert [e["amount"] for e in created.json()["entries"]] == [-3_000, 3_000]
    assert retried.status_code == 200
    assert retried.json() == created.json()
    assert again.status_code == 409
    assert balance(client, customer)["posted"] == 0
    assert (
        client.get(f"/transactions/{original['id']}").json()["reversed_by"]
        == (created.json()["id"])
    )


def test_a_reversal_cannot_be_reversed(client: TestClient) -> None:
    clearing, customer = open_account(client, "asset"), open_account(client, "liability")
    original = client.post("/transactions", json=funding(clearing, customer)).json()
    reversed_ = client.post(f"/transactions/{original['id']}/reversal", json=reversal()).json()

    response = client.post(f"/transactions/{reversed_['id']}/reversal", json=reversal())

    assert response.status_code == 409


def test_reversing_an_unknown_transaction_is_not_found(client: TestClient) -> None:
    assert client.post(f"/transactions/{2**62}/reversal", json=reversal()).status_code == 404


def test_settling_a_pending_transaction_once(client: TestClient) -> None:
    clearing, customer = open_account(client, "asset"), open_account(client, "liability")
    pending = client.post(
        "/transactions", json={**funding(clearing, customer, 3_000), "status": "pending"}
    ).json()
    body = {
        "idempotency_key": unique("settlement"),
        "actor": SETTLEMENT_JOB,
        "effective_at": "2026-03-02T21:00:00Z",
    }
    before = balance(client, customer)

    created = client.post(f"/transactions/{pending['id']}/settlement", json=body)
    retried = client.post(f"/transactions/{pending['id']}/settlement", json=body)
    again = client.post(
        f"/transactions/{pending['id']}/settlement",
        json={**body, "idempotency_key": unique("settlement")},
    )

    assert pending["status"] == "pending"
    assert (before["posted"], before["pending"], before["available"]) == (0, 3_000, 0)
    assert created.status_code == 201
    assert created.json()["settlement"]["actor"] == SETTLEMENT_JOB
    assert retried.status_code == 200
    assert retried.headers["Idempotent-Replayed"] == "true"
    assert retried.json() == created.json()
    assert again.status_code == 409
    after = balance(client, customer)
    assert (after["posted"], after["pending"], after["available"]) == (3_000, 0, 3_000)


def test_settling_a_posted_transaction_is_unprocessable(client: TestClient) -> None:
    clearing, customer = open_account(client, "asset"), open_account(client, "liability")
    posted = client.post("/transactions", json=funding(clearing, customer)).json()

    response = client.post(
        f"/transactions/{posted['id']}/settlement",
        json={
            "idempotency_key": unique("settlement"),
            "actor": SETTLEMENT_JOB,
            "effective_at": "2026-03-02T21:00:00Z",
        },
    )

    assert response.status_code == 422


def place_hold(client: TestClient, account: str, amount: int = 1_000) -> dict[str, Any]:
    return {
        "idempotency_key": unique("hold"),
        "actor": SETTLEMENT_JOB,
        "effective_at": "2026-03-02T21:00:00Z",
        "account": account,
        "currency": "USD",
        "amount": amount,
    }


def test_placing_and_releasing_a_hold(client: TestClient) -> None:
    clearing, customer = open_account(client, "asset"), open_account(client, "liability")
    client.post("/transactions", json=funding(clearing, customer, 3_000))
    body = place_hold(client, customer, 1_000)

    created = client.post("/holds", json=body)
    retried = client.post("/holds", json=body)
    held = balance(client, customer)
    release = {
        "idempotency_key": unique("release"),
        "actor": SETTLEMENT_JOB,
        "effective_at": "2026-03-02T21:00:00Z",
    }
    released = client.post(f"/holds/{created.json()['id']}/release", json=release)
    released_again = client.post(f"/holds/{created.json()['id']}/release", json=release)
    other_release = client.post(
        f"/holds/{created.json()['id']}/release",
        json={**release, "idempotency_key": unique("release")},
    )

    assert created.status_code == 201
    assert created.headers["Location"] == f"/holds/{created.json()['id']}"
    assert created.json()["ended"] is None
    assert retried.status_code == 200
    assert retried.json() == created.json()
    assert (held["posted"], held["held"], held["available"]) == (3_000, 1_000, 2_000)
    assert released.status_code == 201
    assert released.json()["ended"]["outcome"] == "released"
    assert released_again.status_code == 200
    assert other_release.status_code == 409
    assert client.get(f"/holds/{created.json()['id']}").json() == released.json()
    assert balance(client, customer)["available"] == 3_000


def test_capturing_a_hold(client: TestClient) -> None:
    clearing, customer = open_account(client, "asset"), open_account(client, "liability")
    client.post("/transactions", json=funding(clearing, customer, 3_000))
    hold_id = client.post("/holds", json=place_hold(client, customer, 1_000)).json()["id"]

    captured = client.post(f"/holds/{hold_id}/capture", json=funding(customer, clearing, 1_000))

    assert captured.status_code == 201
    assert captured.headers["Location"] == f"/transactions/{captured.json()['id']}"
    assert client.get(f"/holds/{hold_id}").json()["ended"]["transaction"] == captured.json()["id"]
    after = balance(client, customer)
    assert (after["posted"], after["held"], after["available"]) == (2_000, 0, 2_000)


@pytest.mark.parametrize("amount", [0, -5, 10.0, "10"], ids=repr)
def test_a_hold_amount_is_a_positive_json_integer(client: TestClient, amount: Any) -> None:
    body = place_hold(client, open_account(client, "liability"))
    body["amount"] = amount

    assert client.post("/holds", json=body).status_code == 422


def test_an_unknown_hold_is_not_found(client: TestClient) -> None:
    assert client.get(f"/holds/{2**62}").status_code == 404
