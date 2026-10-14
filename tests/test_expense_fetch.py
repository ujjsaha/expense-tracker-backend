"""Date-range listing: filtering, sorting, pagination (SRS 5.6) and lookup by id (5.7)."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.conftest import SEEDED_EXPENSE_COUNT


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _post(client: TestClient, headers: dict, **body):
    return client.post("/expense/fetch_expense", json=body, headers=headers)


def _fetch(client: TestClient, headers: dict, **body) -> dict:
    response = _post(client, headers, **body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def full_range(seed_start_date) -> dict[str, str]:
    """Widened to whole days so the seeded records at 12:00 all fall inside."""
    start = seed_start_date.replace(hour=0, minute=0)
    end = (seed_start_date + timedelta(days=SEEDED_EXPENSE_COUNT - 1)).replace(hour=23, minute=59)
    return {"from_date": _iso(start), "to_date": _iso(end)}


# --------------------------------------------------------------------------- #
# Range filtering
# --------------------------------------------------------------------------- #


def test_full_range_returns_every_record(
    client: TestClient, auth_headers: dict, seeded_expenses: list, full_range: dict
) -> None:
    body = _fetch(client, auth_headers, **full_range, page_size=100)

    assert body["status"] == "success"
    assert body["total_records"] == SEEDED_EXPENSE_COUNT
    assert len(body["data"]) == SEEDED_EXPENSE_COUNT


def test_range_bounds_are_inclusive_at_both_ends(
    client: TestClient, auth_headers: dict, seeded_expenses: list, seed_start_date
) -> None:
    """A bound landing exactly on a record's instant includes that record."""
    first = seed_start_date
    last = seed_start_date + timedelta(days=SEEDED_EXPENSE_COUNT - 1)

    first_only = _fetch(client, auth_headers, from_date=_iso(first), to_date=_iso(first))
    last_only = _fetch(client, auth_headers, from_date=_iso(last), to_date=_iso(last))

    assert first_only["total_records"] == 1
    assert first_only["data"][0]["expense_name"] == "Expense 00"
    assert last_only["total_records"] == 1
    assert last_only["data"][0]["expense_name"] == f"Expense {SEEDED_EXPENSE_COUNT - 1:02d}"


def test_to_date_late_in_the_day_includes_late_expenses(
    client: TestClient, auth_headers: dict, user_a: dict, db, seed_start_date
) -> None:
    """An explicit end-of-day upper bound reaches records just before midnight."""
    from app.models.expense import Expense

    late = seed_start_date.replace(hour=23, minute=59)
    db.add(
        Expense(
            user_id=user_a["user_id"],
            expense_category="Food",
            expense_name="Late night snack",
            amount_minor=5000,
            date_time=late,
        )
    )
    db.commit()

    body = _fetch(
        client,
        auth_headers,
        from_date=_iso(late.replace(hour=13, minute=0)),
        to_date=_iso(late.replace(hour=23, minute=59, second=59)),
    )

    assert [item["expense_name"] for item in body["data"]] == ["Late night snack"]


def test_empty_range_is_200_with_an_empty_list(
    client: TestClient, auth_headers: dict, seeded_expenses: list, seed_start_date
) -> None:
    """SRS 5.6: never a 404."""
    long_ago = _iso(seed_start_date - timedelta(days=400))

    body = _fetch(client, auth_headers, from_date=long_ago, to_date=long_ago)

    assert body["data"] == []
    assert body["total_records"] == 0
    assert body["total_pages"] == 0


def test_to_date_is_inclusive_of_exactly_that_instant(
    client: TestClient, auth_headers: dict, seeded_expenses: list, seed_start_date
) -> None:
    """The seeded records sit at 12:00.

    A window stopping at 11:59 excludes the day it ends on; one stopping at 12:00
    includes it.
    """
    day = seed_start_date
    midnight = _iso(day.replace(hour=0, minute=0))

    before = _fetch(
        client, auth_headers, from_date=midnight, to_date=_iso(day.replace(hour=11, minute=59))
    )
    exactly = _fetch(client, auth_headers, from_date=midnight, to_date=_iso(day))

    assert before["total_records"] == 0
    assert exactly["total_records"] == 1


def test_bare_date_bounds_are_read_as_midnight_utc(
    client: TestClient, auth_headers: dict, seeded_expenses: list, seed_start_date
) -> None:
    """A date-only bound is accepted and means 00:00:00Z - not the whole day.

    That is the deliberate consequence of `to_date` being an instant: the seeded
    record at 12:00 falls outside a window ending at that day's midnight.
    """
    day = seed_start_date.date().isoformat()

    body = _fetch(client, auth_headers, from_date=day, to_date=day)

    assert body["total_records"] == 0


def test_naive_datetime_bounds_are_treated_as_utc(
    client: TestClient, auth_headers: dict, seeded_expenses: list, seed_start_date
) -> None:
    naive = seed_start_date.replace(tzinfo=None).isoformat()

    body = _fetch(client, auth_headers, from_date=naive, to_date=naive)

    assert body["total_records"] == 1


def test_offset_bounds_are_converted_to_utc(
    client: TestClient, auth_headers: dict, seeded_expenses: list, seed_start_date
) -> None:
    """12:00Z expressed as +05:30 is the same instant, so the record still matches."""
    from datetime import timezone

    shifted = seed_start_date.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()

    body = _fetch(client, auth_headers, from_date=shifted, to_date=shifted)

    assert body["total_records"] == 1


@pytest.mark.parametrize(
    "value",
    ["not-a-date", "2026-13-01", "2026-08-01T99:00:00Z", "01/08/2026", "", None],
)
def test_malformed_range_bounds_are_422(
    client: TestClient, auth_headers: dict, seed_start_date, value: str | None
) -> None:
    response = _post(client, auth_headers, from_date=value, to_date=_iso(seed_start_date))

    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["body", "from_date"]


@pytest.mark.parametrize("field", ["from_date", "to_date"])
def test_missing_range_bound_is_422(
    client: TestClient, auth_headers: dict, seed_start_date, field: str
) -> None:
    body = {"from_date": _iso(seed_start_date), "to_date": _iso(seed_start_date)}
    del body[field]

    response = _post(client, auth_headers, **body)

    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["body", field]


def test_to_datetime_before_from_datetime_is_400(
    client: TestClient, auth_headers: dict, seed_start_date
) -> None:
    """The 400 rule still applies when the bounds differ only by time of day."""
    response = _post(
        client,
        auth_headers,
        from_date=_iso(seed_start_date.replace(hour=18)),
        to_date=_iso(seed_start_date.replace(hour=9)),
    )

    assert response.status_code == 400


def test_to_date_before_from_date_is_400(
    client: TestClient, auth_headers: dict, seed_start_date
) -> None:
    response = _post(
        client,
        auth_headers,
        from_date=_iso(seed_start_date),
        to_date=_iso(seed_start_date - timedelta(days=5)),
    )

    assert response.status_code == 400
    assert response.json()["message"] == "to_date cannot be earlier than from_date"


def test_equal_bounds_are_allowed(
    client: TestClient, auth_headers: dict, seeded_expenses: list, seed_start_date
) -> None:
    """`to_date == from_date` is a valid, single-instant range."""
    moment = _iso(seed_start_date)

    assert _fetch(client, auth_headers, from_date=moment, to_date=moment)["total_records"] == 1


def test_another_users_expenses_are_never_returned(
    client: TestClient,
    other_auth_headers: dict,
    seeded_expenses: list,
    full_range: dict,
) -> None:
    body = _fetch(client, other_auth_headers, **full_range, page_size=100)

    assert body["total_records"] == 0


def test_fetch_requires_authentication(client: TestClient, full_range: dict) -> None:
    assert client.post("/expense/fetch_expense", json=full_range).status_code == 401


# --------------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("sort_by", ["date_time", "amount", "expense_category", "expense_name"])
@pytest.mark.parametrize("sort_order", ["ASC", "DESC"])
def test_sorting_is_applied(
    client: TestClient,
    auth_headers: dict,
    seeded_expenses: list,
    full_range: dict,
    sort_by: str,
    sort_order: str,
) -> None:
    body = _fetch(
        client, auth_headers, **full_range, sort_by=sort_by, sort_order=sort_order, page_size=100
    )

    values = [item[sort_by] for item in body["data"]]
    assert values == sorted(values, reverse=sort_order == "DESC")


def test_default_sort_is_date_time_descending(
    client: TestClient, auth_headers: dict, seeded_expenses: list, full_range: dict
) -> None:
    body = _fetch(client, auth_headers, **full_range, page_size=100)

    names = [item["expense_name"] for item in body["data"]]
    assert names[0] == f"Expense {SEEDED_EXPENSE_COUNT - 1:02d}"
    assert names[-1] == "Expense 00"


def test_amount_sorts_numerically_not_lexicographically(
    client: TestClient, auth_headers: dict, seeded_expenses: list, full_range: dict
) -> None:
    """A string-backed amount column would put 9.99 after 100.00."""
    body = _fetch(
        client, auth_headers, **full_range, sort_by="amount", sort_order="ASC", page_size=100
    )

    amounts = [Decimal(str(item["amount"])) for item in body["data"]]
    assert amounts == sorted(amounts)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sort_by", "password"),
        ("sort_by", "user_id"),
        ("sort_by", "DATE_TIME"),
        ("sort_order", "sideways"),
        ("sort_order", "asc"),
        ("sort_order", "desc"),
    ],
)
def test_invalid_sort_parameters_are_422(
    client: TestClient, auth_headers: dict, full_range: dict, field: str, value: str
) -> None:
    """`sort_order` is upper case and `sort_by` lower case - neither is coerced."""
    response = _post(client, auth_headers, **{**full_range, field: value})

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #


def test_pagination_counters(
    client: TestClient, auth_headers: dict, seeded_expenses: list, full_range: dict
) -> None:
    body = _fetch(client, auth_headers, **full_range, page=1, page_size=10)

    assert body["page"] == 1
    assert body["page_size"] == 10
    assert body["total_records"] == SEEDED_EXPENSE_COUNT
    assert body["total_pages"] == 3  # ceil(25 / 10)
    assert len(body["data"]) == 10


def test_last_page_is_partial(
    client: TestClient, auth_headers: dict, seeded_expenses: list, full_range: dict
) -> None:
    body = _fetch(client, auth_headers, **full_range, page=3, page_size=10)

    assert len(body["data"]) == 5


def test_pages_do_not_overlap_and_cover_everything(
    client: TestClient, auth_headers: dict, seeded_expenses: list, full_range: dict
) -> None:
    seen: list[int] = []
    for page in (1, 2, 3):
        body = _fetch(client, auth_headers, **full_range, page=page, page_size=10)
        seen.extend(item["expense_id"] for item in body["data"])

    assert len(seen) == len(set(seen)) == SEEDED_EXPENSE_COUNT


def test_page_beyond_the_end_is_empty(
    client: TestClient, auth_headers: dict, seeded_expenses: list, full_range: dict
) -> None:
    body = _fetch(client, auth_headers, **full_range, page=99, page_size=10)

    assert body["data"] == []
    assert body["total_records"] == SEEDED_EXPENSE_COUNT


@pytest.mark.parametrize(
    ("field", "value"),
    [("page", 0), ("page", -1), ("page_size", 0), ("page_size", 101)],
)
def test_out_of_bounds_pagination_is_422(
    client: TestClient, auth_headers: dict, full_range: dict, field: str, value: int
) -> None:
    response = _post(client, auth_headers, **{**full_range, field: value})

    assert response.status_code == 422


def test_defaults_are_applied_when_optional_fields_are_omitted(
    client: TestClient, auth_headers: dict, seeded_expenses: list, full_range: dict
) -> None:
    """Only the two bounds are required; the rest fall back to their defaults."""
    body = _fetch(client, auth_headers, **full_range)

    assert body["page"] == 1
    assert body["page_size"] == 10
    # date_time DESC is the default ordering.
    assert body["data"][0]["expense_name"] == f"Expense {SEEDED_EXPENSE_COUNT - 1:02d}"


def test_missing_body_is_422(client: TestClient, auth_headers: dict) -> None:
    assert client.post("/expense/fetch_expense", headers=auth_headers).status_code == 422


# --------------------------------------------------------------------------- #
# Fetch by id (SRS 5.7)
# --------------------------------------------------------------------------- #


def test_fetch_expense_by_id(
    client: TestClient, auth_headers: dict, user_a: dict, seeded_expenses: list
) -> None:
    expected = seeded_expenses[0]

    response = client.get(
        f"/expense/fetch_expense_by_id?expense_id={expected['expense_id']}", headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["expense_id"] == expected["expense_id"]
    assert body["user_id"] == user_a["user_id"]
    assert Decimal(str(body["amount"])) == expected["amount"]
    assert body["expense_name"] == expected["expense_name"]
    assert body["date_time"].endswith("Z")


def test_fetch_another_users_expense_is_403(
    client: TestClient, other_auth_headers: dict, seeded_expenses: list
) -> None:
    response = client.get(
        f"/expense/fetch_expense_by_id?expense_id={seeded_expenses[0]['expense_id']}",
        headers=other_auth_headers,
    )

    assert response.status_code == 403


def test_fetch_unknown_expense_is_404(client: TestClient, auth_headers: dict) -> None:
    response = client.get("/expense/fetch_expense_by_id?expense_id=999999", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["message"] == "Expense not found"


def test_fetch_by_id_requires_authentication(client: TestClient, seeded_expenses: list) -> None:
    response = client.get(
        f"/expense/fetch_expense_by_id?expense_id={seeded_expenses[0]['expense_id']}"
    )

    assert response.status_code == 401
