"""Expense creation (SRS 5.5) and the section 3.2 validation rules."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.models.expense import Expense


def _payload(**overrides) -> dict:
    base = {
        "expense_category": "Food",
        "expense_name": "Team lunch",
        "amount": 1250.50,
        "date_time": (utc_now() - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }
    return base | overrides


def test_create_expense_returns_201(client: TestClient, auth_headers: dict) -> None:
    response = client.post("/expense/create_expense", json=_payload(), headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "success"
    assert body["message"] == "Expense created successfully"
    assert isinstance(body["expense_id"], int)


def test_amount_is_stored_exactly(client: TestClient, auth_headers: dict, db: Session) -> None:
    """Integer minor units, so no float drift on a value like 1250.50."""
    client.post("/expense/create_expense", json=_payload(amount=1250.50), headers=auth_headers)

    expense = db.query(Expense).one()
    assert expense.amount_minor == 125050
    assert str(expense.amount) == "1250.50"


def test_user_id_comes_from_the_token_not_the_body(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict, db: Session
) -> None:
    """SRS 3.3: a user_id in the request body must be ignored."""
    response = client.post(
        "/expense/create_expense",
        json=_payload(user_id=user_b["user_id"]),
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert db.query(Expense).one().user_id == user_a["user_id"]


def test_create_requires_authentication(client: TestClient) -> None:
    assert client.post("/expense/create_expense", json=_payload()).status_code == 401


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"amount": 0}, "amount must be greater than zero"),
        ({"amount": -10.50}, "amount must be positive"),
        ({"amount": 10.999}, "more than 2 decimal places"),
        ({"expense_name": ""}, "name must be at least 1 character"),
        ({"expense_name": "   "}, "whitespace-only name"),
        ({"expense_name": "x" * 101}, "name longer than 100 characters"),
        ({"expense_category": "Groceries and more"}, "category outside the defined list"),
        ({"expense_category": ""}, "category must be non-empty"),
        ({"date_time": "not-a-date"}, "malformed datetime"),
    ],
)
def test_field_rules_are_enforced(
    client: TestClient, auth_headers: dict, overrides: dict, reason: str
) -> None:
    response = client.post(
        "/expense/create_expense", json=_payload(**overrides), headers=auth_headers
    )

    assert response.status_code == 422, f"expected rejection: {reason}"
    assert response.json()["errors"]


def test_future_date_time_is_rejected(client: TestClient, auth_headers: dict) -> None:
    future = (utc_now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")

    response = client.post(
        "/expense/create_expense", json=_payload(date_time=future), headers=auth_headers
    )

    assert response.status_code == 422
    assert "future" in str(response.json()["errors"]).lower()


def test_small_clock_skew_is_tolerated(client: TestClient, auth_headers: dict) -> None:
    """A client whose clock is a few seconds fast should not be rejected."""
    slightly_ahead = (utc_now() + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")

    response = client.post(
        "/expense/create_expense", json=_payload(date_time=slightly_ahead), headers=auth_headers
    )

    assert response.status_code == 201


def test_missing_required_fields_is_422(client: TestClient, auth_headers: dict) -> None:
    response = client.post("/expense/create_expense", json={}, headers=auth_headers)

    assert response.status_code == 422
    reported = {tuple(error["loc"])[-1] for error in response.json()["errors"]}
    assert {"expense_category", "expense_name", "amount", "date_time"} <= reported


def test_naive_datetime_is_treated_as_utc(
    client: TestClient, auth_headers: dict, db: Session
) -> None:
    naive = (utc_now() - timedelta(days=1)).replace(tzinfo=None).isoformat()

    response = client.post(
        "/expense/create_expense", json=_payload(date_time=naive), headers=auth_headers
    )

    assert response.status_code == 201
    stored = db.query(Expense).one().date_time
    assert stored.tzinfo is not None
    assert stored == datetime.fromisoformat(naive).replace(tzinfo=UTC)


def test_all_defined_categories_are_accepted(client: TestClient, auth_headers: dict) -> None:
    from app.models.expense import ExpenseCategory

    for category in ExpenseCategory:
        response = client.post(
            "/expense/create_expense",
            json=_payload(expense_category=category.value),
            headers=auth_headers,
        )
        assert response.status_code == 201, category
