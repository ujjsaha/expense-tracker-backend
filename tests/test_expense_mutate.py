"""Expense update and delete - the SRS section 8 items brought into scope."""

from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.models.expense import Expense


def test_update_changes_only_the_supplied_fields(
    client: TestClient, auth_headers: dict, seeded_expenses: list, db: Session
) -> None:
    target = seeded_expenses[0]

    response = client.put(
        f"/expense/update_expense?expense_id={target['expense_id']}",
        json={"expense_name": "Renamed lunch"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["expense_name"] == "Renamed lunch"
    # Untouched fields survive.
    assert Decimal(str(body["amount"])) == target["amount"]
    assert body["expense_category"] == target["expense_category"].value

    db.expire_all()
    assert db.get(Expense, target["expense_id"]).expense_name == "Renamed lunch"


def test_update_persists_amount_exactly(
    client: TestClient, auth_headers: dict, seeded_expenses: list, db: Session
) -> None:
    target = seeded_expenses[0]

    response = client.put(
        f"/expense/update_expense?expense_id={target['expense_id']}",
        json={"amount": 99.05},
        headers=auth_headers,
    )

    assert response.status_code == 200
    db.expire_all()
    assert db.get(Expense, target["expense_id"]).amount_minor == 9905


def test_update_all_fields(client: TestClient, auth_headers: dict, seeded_expenses: list) -> None:
    target = seeded_expenses[0]
    when = (utc_now() - timedelta(days=2)).replace(microsecond=0)

    response = client.put(
        f"/expense/update_expense?expense_id={target['expense_id']}",
        json={
            "expense_category": "Travel",
            "expense_name": "Airport taxi",
            "amount": 450.75,
            "date_time": when.isoformat().replace("+00:00", "Z"),
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["expense_category"] == "Travel"
    assert body["expense_name"] == "Airport taxi"
    assert Decimal(str(body["amount"])) == Decimal("450.75")


def test_update_with_no_fields_is_422(
    client: TestClient, auth_headers: dict, seeded_expenses: list
) -> None:
    response = client.put(
        f"/expense/update_expense?expense_id={seeded_expenses[0]['expense_id']}",
        json={},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_update_rejects_invalid_values(
    client: TestClient, auth_headers: dict, seeded_expenses: list
) -> None:
    response = client.put(
        f"/expense/update_expense?expense_id={seeded_expenses[0]['expense_id']}",
        json={"amount": -5},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_update_rejects_a_future_date(
    client: TestClient, auth_headers: dict, seeded_expenses: list
) -> None:
    future = (utc_now() + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    response = client.put(
        f"/expense/update_expense?expense_id={seeded_expenses[0]['expense_id']}",
        json={"date_time": future},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_update_another_users_expense_is_403(
    client: TestClient, other_auth_headers: dict, seeded_expenses: list, db: Session
) -> None:
    target = seeded_expenses[0]

    response = client.put(
        f"/expense/update_expense?expense_id={target['expense_id']}",
        json={"expense_name": "Hijacked"},
        headers=other_auth_headers,
    )

    assert response.status_code == 403
    db.expire_all()
    assert db.get(Expense, target["expense_id"]).expense_name == target["expense_name"]


def test_update_unknown_expense_is_404(client: TestClient, auth_headers: dict) -> None:
    response = client.put(
        "/expense/update_expense?expense_id=999999",
        json={"expense_name": "Nothing"},
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_update_requires_authentication(client: TestClient, seeded_expenses: list) -> None:
    response = client.put(
        f"/expense/update_expense?expense_id={seeded_expenses[0]['expense_id']}",
        json={"expense_name": "Anonymous"},
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


def test_delete_removes_the_expense(
    client: TestClient, auth_headers: dict, seeded_expenses: list
) -> None:
    expense_id = seeded_expenses[0]["expense_id"]

    response = client.delete(
        f"/expense/delete_expense?expense_id={expense_id}", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["message"] == "Expense deleted successfully"
    assert (
        client.get(
            f"/expense/fetch_expense_by_id?expense_id={expense_id}", headers=auth_headers
        ).status_code
        == 404
    )


def test_delete_is_not_repeatable(
    client: TestClient, auth_headers: dict, seeded_expenses: list
) -> None:
    expense_id = seeded_expenses[0]["expense_id"]
    client.delete(f"/expense/delete_expense?expense_id={expense_id}", headers=auth_headers)

    response = client.delete(
        f"/expense/delete_expense?expense_id={expense_id}", headers=auth_headers
    )

    assert response.status_code == 404


def test_delete_another_users_expense_is_403_and_leaves_the_row(
    client: TestClient, other_auth_headers: dict, seeded_expenses: list, db: Session
) -> None:
    expense_id = seeded_expenses[0]["expense_id"]

    response = client.delete(
        f"/expense/delete_expense?expense_id={expense_id}", headers=other_auth_headers
    )

    assert response.status_code == 403
    db.expire_all()
    assert db.get(Expense, expense_id) is not None


def test_delete_unknown_expense_is_404(client: TestClient, auth_headers: dict) -> None:
    assert (
        client.delete("/expense/delete_expense?expense_id=999999", headers=auth_headers).status_code
        == 404
    )


def test_delete_requires_authentication(client: TestClient, seeded_expenses: list) -> None:
    response = client.delete(
        f"/expense/delete_expense?expense_id={seeded_expenses[0]['expense_id']}"
    )

    assert response.status_code == 401


def test_deleting_a_user_cascades_to_their_expenses(
    client: TestClient, user_a: dict, seeded_expenses: list, db: Session
) -> None:
    """Verifies the SQLite foreign_keys pragma is actually on."""
    from app.models.user import User

    db.delete(db.get(User, user_a["user_id"]))
    db.commit()

    assert db.query(Expense).count() == 0
