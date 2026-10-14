"""User registration, profile lookup and password reset (SRS 5.1, 5.2, 5.4)."""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.models.auth import PasswordResetOTP
from tests.conftest import USER_A, USER_B, bearer, login, register

PASSWORD_FIELD_NAMES = {"password", "confirm_password", "password_hash", "otp_hash"}


def _find_password_fields(payload: Any, path: str = "") -> list[str]:
    """Walk a response body looking for anything password-shaped."""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in PASSWORD_FIELD_NAMES:
                found.append(f"{path}.{key}")
            found.extend(_find_password_fields(value, f"{path}.{key}"))
    elif isinstance(payload, list):
        for position, item in enumerate(payload):
            found.extend(_find_password_fields(item, f"{path}[{position}]"))
    return found


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_register_returns_201_and_user_id(client: TestClient) -> None:
    response = client.post("/user/register_user", json=USER_A)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "success"
    assert body["message"] == "User registered successfully"
    assert isinstance(body["user_id"], int)


def test_register_with_mismatched_confirm_password_is_400(client: TestClient) -> None:
    """SRS 5.1 requires 400 here, not the 422 a schema validator would produce."""
    payload = USER_A | {"confirm_password": "Different@1"}

    response = client.post("/user/register_user", json=payload)

    assert response.status_code == 400
    assert response.json() == {
        "status": "failure",
        "message": "Password and confirm password do not match",
        "errors": [],
    }


def test_registering_an_existing_email_updates_the_user_instead_of_409(
    client: TestClient, db: Session
) -> None:
    from app.models.user import User

    original_id = register(client, USER_A)
    payload = USER_A | {"name": "Priya Kumar", "mobile": "9000000001"}

    response = client.post("/user/register_user", json=payload)

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "message": "User updated successfully",
        "user_id": original_id,
    }
    assert db.query(User).count() == 1
    updated = db.query(User).one()
    assert updated.name == "Priya Kumar"
    assert updated.mobile == "9000000001"


def test_duplicate_mobile_is_409(client: TestClient) -> None:
    register(client, USER_A)
    payload = USER_A | {"email": "different@example.com"}

    response = client.post("/user/register_user", json=payload)

    assert response.status_code == 409
    assert response.json()["message"] == "Mobile number already registered"


def test_email_uniqueness_is_case_insensitive(client: TestClient, db: Session) -> None:
    """An uppercase variant of an existing email still matches - it updates,
    it does not create a second account."""
    from app.models.user import User

    register(client, USER_A)
    payload = USER_A | {"email": USER_A["email"].upper(), "mobile": "9000000002"}

    response = client.post("/user/register_user", json=payload)

    assert response.status_code == 200
    assert db.query(User).count() == 1


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("name", "A", "shorter than 2 characters"),
        ("name", "A" * 51, "longer than 50 characters"),
        ("name", "Priya99", "digits are not alphabetic"),
        ("name", "Priya_Sharma", "underscore is not a space"),
        ("mobile", "12345", "fewer than 10 digits"),
        ("mobile", "12345678901", "more than 10 digits"),
        ("mobile", "98765abcde", "not numeric"),
        ("mobile", "+0123456789", "E.164 cannot start with zero"),
        ("email", "not-an-email", "malformed address"),
        ("email", "missing@domain", "no top level domain"),
        ("password", "Sh0rt@1", "fewer than 8 characters"),
        ("password", "str0ng@pass", "no uppercase letter"),
        ("password", "STR0NG@PASS", "no lowercase letter"),
        ("password", "Strong@Pass", "no digit"),
        ("password", "Str0ngPass1", "no special character"),
        ("password", "A1@" + "a" * 70, "longer than 72 bytes"),
    ],
)
def test_registration_field_rules_are_enforced(
    client: TestClient, field: str, value: str, reason: str
) -> None:
    payload = USER_A | {field: value}
    if field == "password":
        payload["confirm_password"] = value

    response = client.post("/user/register_user", json=payload)

    assert response.status_code == 422, f"expected rejection: {reason}"
    body = response.json()
    assert body["status"] == "failure"
    assert body["errors"], "validation detail should be reported in `errors`"


def test_valid_e164_mobile_is_accepted(client: TestClient) -> None:
    payload = USER_A | {"mobile": "+919876543211"}

    assert client.post("/user/register_user", json=payload).status_code == 201


def test_name_whitespace_is_collapsed(client: TestClient, db: Session) -> None:
    from app.models.user import User

    register(client, USER_A | {"name": "  Priya   Sharma  "})

    assert db.query(User).one().name == "Priya Sharma"


def test_register_defaults_user_type_and_status(client: TestClient, db: Session) -> None:
    from app.models.user import User

    register(client, USER_A)

    user = db.query(User).one()
    assert user.user_type == "member"
    assert user.status == "active"


def test_register_accepts_explicit_user_type_and_status(client: TestClient, db: Session) -> None:
    from app.models.user import User

    register(client, USER_A | {"user_type": "admin", "status": "inactive"})

    user = db.query(User).one()
    assert user.user_type == "admin"
    assert user.status == "inactive"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_type", "owner"),
        ("status", "banned"),
    ],
)
def test_register_rejects_invalid_user_type_or_status(
    client: TestClient, field: str, value: str
) -> None:
    response = client.post("/user/register_user", json=USER_A | {field: value})

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Register-as-update (existing email)
# --------------------------------------------------------------------------- #


def test_re_registering_updates_user_type_and_status(client: TestClient, db: Session) -> None:
    from app.models.user import User

    register(client, USER_A)

    response = client.post(
        "/user/register_user", json=USER_A | {"user_type": "admin", "status": "inactive"}
    )

    assert response.status_code == 200
    user = db.query(User).one()
    assert user.user_type == "admin"
    assert user.status == "inactive"


def test_re_registering_ignores_password_and_confirm_password(
    client: TestClient, db: Session
) -> None:
    """Password fields are immutable on the update path (SRS 5.1): supplying
    a new, even mismatched, pair changes nothing and does not error."""
    from app.models.user import User

    register(client, USER_A)
    original_hash = db.query(User).one().password_hash

    response = client.post(
        "/user/register_user",
        json=USER_A | {"password": "Different@Pass1", "confirm_password": "Mismatched@Pass2"},
    )

    assert response.status_code == 200
    assert db.query(User).one().password_hash == original_hash
    # The original password still logs in; the request's password never applied.
    login(client, USER_A["email"], USER_A["password"])


def test_re_registering_with_a_different_email_cannot_rename_the_account(
    client: TestClient, db: Session
) -> None:
    """`email` is immutable and doubles as the lookup key, so there is no way
    through this endpoint to rename an existing account's email - a different
    email simply does not match it, and is handled as a fresh registration."""
    from app.models.user import User

    register(client, USER_A)

    response = client.post(
        "/user/register_user",
        json=USER_A | {"email": "someone-else@example.com", "mobile": "9000000006"},
    )

    assert response.status_code == 201
    assert db.query(User).count() == 2
    assert db.query(User).filter_by(email=USER_A["email"]).one().mobile == USER_A["mobile"]


def test_re_registering_updates_mobile(client: TestClient, db: Session) -> None:
    from app.models.user import User

    register(client, USER_A)

    response = client.post("/user/register_user", json=USER_A | {"mobile": "9000000005"})

    assert response.status_code == 200
    assert db.query(User).one().mobile == "9000000005"


def test_re_registering_with_another_users_mobile_is_409(
    client: TestClient, db: Session
) -> None:
    from app.models.user import User

    register(client, USER_A)
    register(client, USER_B)

    response = client.post("/user/register_user", json=USER_A | {"mobile": USER_B["mobile"]})

    assert response.status_code == 409
    assert response.json()["message"] == "Mobile number already registered"
    # The attempted update did not partially apply.
    assert db.query(User).filter_by(email=USER_A["email"]).one().mobile == USER_A["mobile"]


# --------------------------------------------------------------------------- #
# Password leakage
# --------------------------------------------------------------------------- #


def test_no_endpoint_response_contains_a_password_field(
    client: TestClient, user_a: dict, auth_headers: dict
) -> None:
    """SRS 3.3: password fields are write-only."""
    responses = [
        client.post("/user/register_user", json=USER_B),
        client.post(
            "/user/login_user", json={"email": USER_A["email"], "password": USER_A["password"]}
        ),
        client.get(f"/user/fetch_user_by_id?user_id={user_a['user_id']}", headers=auth_headers),
        client.post("/user/refresh_token", json={"refresh_token": user_a["refresh_token"]}),
        client.post("/user/fetch_all_users", json={}, headers=auth_headers),
    ]

    for response in responses:
        leaked = _find_password_fields(response.json())
        assert not leaked, f"{response.url} leaked {leaked}"


# --------------------------------------------------------------------------- #
# Profile lookup
# --------------------------------------------------------------------------- #


def test_fetch_own_profile(client: TestClient, user_a: dict, auth_headers: dict) -> None:
    response = client.get(
        f"/user/fetch_user_by_id?user_id={user_a['user_id']}", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": user_a["user_id"],
        "name": USER_A["name"],
        "mobile": USER_A["mobile"],
        "email": USER_A["email"],
    }


def test_fetch_another_users_profile_is_403(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict
) -> None:
    response = client.get(
        f"/user/fetch_user_by_id?user_id={user_b['user_id']}", headers=auth_headers
    )

    assert response.status_code == 403


def test_fetch_unknown_user_is_404(client: TestClient, auth_headers: dict) -> None:
    response = client.get("/user/fetch_user_by_id?user_id=999999", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["message"] == "User not found"


# --------------------------------------------------------------------------- #
# Activate / deactivate user
# --------------------------------------------------------------------------- #


def test_admin_deactivate_own_account_sets_status_inactive(
    client: TestClient, user_a: dict, admin_headers: dict, db: Session
) -> None:
    from app.models.user import User

    response = client.post(
        "/user/deactivate_user",
        json={"user_id": user_a["user_id"], "status": "inactive"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "success", "message": "User deactivated successfully"}
    assert db.query(User).filter_by(id=user_a["user_id"]).one().status == "inactive"


def test_admin_activate_own_account_sets_status_active(
    client: TestClient, user_a: dict, admin_headers: dict, db: Session
) -> None:
    from app.models.user import User

    client.post(
        "/user/deactivate_user",
        json={"user_id": user_a["user_id"], "status": "inactive"},
        headers=admin_headers,
    )

    response = client.post(
        "/user/deactivate_user",
        json={"user_id": user_a["user_id"], "status": "active"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "success", "message": "User activated successfully"}
    assert db.query(User).filter_by(id=user_a["user_id"]).one().status == "active"


def test_admin_deactivate_another_users_account_succeeds(
    client: TestClient, user_b: dict, admin_headers: dict, db: Session
) -> None:
    from app.models.user import User

    response = client.post(
        "/user/deactivate_user",
        json={"user_id": user_b["user_id"], "status": "inactive"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert db.query(User).filter_by(id=user_b["user_id"]).one().status == "inactive"


def test_deactivate_requires_authentication(client: TestClient, user_a: dict) -> None:
    response = client.post(
        "/user/deactivate_user", json={"user_id": user_a["user_id"], "status": "inactive"}
    )

    assert response.status_code == 401


def test_deactivate_rejects_garbage_token(client: TestClient, user_a: dict) -> None:
    response = client.post(
        "/user/deactivate_user",
        json={"user_id": user_a["user_id"], "status": "inactive"},
        headers=bearer("garbage"),
    )

    assert response.status_code == 401


def test_non_admin_deactivate_own_account_is_403(
    client: TestClient, user_a: dict, auth_headers: dict, db: Session
) -> None:
    from app.models.user import User

    response = client.post(
        "/user/deactivate_user",
        json={"user_id": user_a["user_id"], "status": "inactive"},
        headers=auth_headers,
    )

    assert response.status_code == 403
    assert db.query(User).filter_by(id=user_a["user_id"]).one().status == "active"


def test_non_admin_deactivate_another_users_account_is_403(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict, db: Session
) -> None:
    from app.models.user import User

    response = client.post(
        "/user/deactivate_user",
        json={"user_id": user_b["user_id"], "status": "inactive"},
        headers=auth_headers,
    )

    assert response.status_code == 403
    assert db.query(User).filter_by(id=user_b["user_id"]).one().status == "active"


def test_deactivate_unknown_user_is_404(client: TestClient, admin_headers: dict) -> None:
    response = client.post(
        "/user/deactivate_user",
        json={"user_id": 999999, "status": "inactive"},
        headers=admin_headers,
    )

    assert response.status_code == 404
    assert response.json()["message"] == "User not found"


def test_deactivate_rejects_invalid_status_value(
    client: TestClient, user_a: dict, admin_headers: dict
) -> None:
    response = client.post(
        "/user/deactivate_user",
        json={"user_id": user_a["user_id"], "status": "banned"},
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json()["status"] == "failure"


def test_deactivate_is_idempotent(
    client: TestClient, user_a: dict, admin_headers: dict, db: Session
) -> None:
    from app.models.user import User

    payload = {"user_id": user_a["user_id"], "status": "inactive"}

    first = client.post("/user/deactivate_user", json=payload, headers=admin_headers)
    second = client.post("/user/deactivate_user", json=payload, headers=admin_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert db.query(User).filter_by(id=user_a["user_id"]).one().status == "inactive"


def test_activate_is_idempotent(
    client: TestClient, user_a: dict, admin_headers: dict, db: Session
) -> None:
    """`user_a` starts active, so both calls are no-ops on an already-active account."""
    from app.models.user import User

    payload = {"user_id": user_a["user_id"], "status": "active"}

    first = client.post("/user/deactivate_user", json=payload, headers=admin_headers)
    second = client.post("/user/deactivate_user", json=payload, headers=admin_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert db.query(User).filter_by(id=user_a["user_id"]).one().status == "active"


# --------------------------------------------------------------------------- #
# List all users
# --------------------------------------------------------------------------- #

USER_C = {
    "name": "Amit Singh",
    "mobile": "9000000099",
    "email": "amit@example.com",
    "password": "Th1rd@Pass",
    "confirm_password": "Th1rd@Pass",
}


def test_fetch_all_users_requires_authentication(client: TestClient) -> None:
    assert client.post("/user/fetch_all_users", json={}).status_code == 401


def test_fetch_all_users_defaults_to_name_ascending(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict
) -> None:
    register(client, USER_C)

    response = client.post("/user/fetch_all_users", json={}, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 10
    assert body["total_records"] == 3
    assert body["total_pages"] == 1
    names = [row["name"] for row in body["data"]]
    assert names == sorted(names)
    for row in body["data"]:
        assert set(row) == {"user_id", "name", "email", "mobile", "user_type", "status"}
        assert row["user_type"] == "member"
        assert row["status"] == "active"


def test_fetch_all_users_sort_by_email_desc(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict
) -> None:
    response = client.post(
        "/user/fetch_all_users",
        json={"sort_by": "email", "sort_order": "DESC"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    emails = [row["email"] for row in response.json()["data"]]
    assert emails == sorted(emails, reverse=True)


def test_fetch_all_users_pagination(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict
) -> None:
    register(client, USER_C)

    first_page = client.post(
        "/user/fetch_all_users", json={"page_size": 1, "page": 1}, headers=auth_headers
    ).json()
    second_page = client.post(
        "/user/fetch_all_users", json={"page_size": 1, "page": 2}, headers=auth_headers
    ).json()

    assert first_page["total_records"] == 3
    assert first_page["total_pages"] == 3
    assert len(first_page["data"]) == 1
    assert len(second_page["data"]) == 1
    assert first_page["data"][0]["user_id"] != second_page["data"][0]["user_id"]


def test_fetch_all_users_search_matches_name_partially_and_case_insensitively(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict
) -> None:
    register(client, USER_C)

    response = client.post(
        "/user/fetch_all_users", json={"search": "PrI"}, headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_records"] == 1
    assert body["data"][0]["user_id"] == user_a["user_id"]


def test_fetch_all_users_search_matches_email_domain_across_users(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict
) -> None:
    response = client.post(
        "/user/fetch_all_users", json={"search": "example.com"}, headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["total_records"] == 2


def test_fetch_all_users_search_matches_mobile(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict
) -> None:
    response = client.post(
        "/user/fetch_all_users", json={"search": "98765"}, headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_records"] == 1
    assert body["data"][0]["user_id"] == user_a["user_id"]


def test_fetch_all_users_search_matches_user_type(
    client: TestClient, user_a: dict, user_b: dict, admin_headers: dict
) -> None:
    """`user_a` is promoted to admin by the `admin_headers` fixture."""
    response = client.post(
        "/user/fetch_all_users", json={"search": "admin"}, headers=admin_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_records"] == 1
    assert body["data"][0]["user_id"] == user_a["user_id"]


def test_fetch_all_users_search_matches_status(
    client: TestClient, user_a: dict, user_b: dict, admin_headers: dict
) -> None:
    client.post(
        "/user/deactivate_user",
        json={"user_id": user_b["user_id"], "status": "inactive"},
        headers=admin_headers,
    )

    response = client.post(
        "/user/fetch_all_users", json={"search": "Inactive"}, headers=admin_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_records"] == 1
    assert body["data"][0]["user_id"] == user_b["user_id"]


def test_fetch_all_users_search_no_match_returns_empty_page(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict
) -> None:
    response = client.post(
        "/user/fetch_all_users", json={"search": "nobody-has-this"}, headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_records"] == 0
    assert body["data"] == []


def test_fetch_all_users_search_blank_or_null_is_unfiltered(
    client: TestClient, user_a: dict, user_b: dict, auth_headers: dict
) -> None:
    blank = client.post("/user/fetch_all_users", json={"search": ""}, headers=auth_headers)
    null = client.post("/user/fetch_all_users", json={"search": None}, headers=auth_headers)
    absent = client.post("/user/fetch_all_users", json={}, headers=auth_headers)

    assert blank.json()["total_records"] == 2
    assert null.json()["total_records"] == 2
    assert absent.json()["total_records"] == 2


def test_fetch_all_users_rejects_invalid_sort_field(client: TestClient, auth_headers: dict) -> None:
    response = client.post(
        "/user/fetch_all_users", json={"sort_by": "password"}, headers=auth_headers
    )

    assert response.status_code == 422


def test_fetch_all_users_rejects_lowercase_sort_order(
    client: TestClient, auth_headers: dict
) -> None:
    response = client.post(
        "/user/fetch_all_users", json={"sort_order": "asc"}, headers=auth_headers
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #


def _request_otp(client: TestClient, mobile: str) -> str:
    response = client.post("/user/forgot_password/request_otp", json={"mobile": mobile})
    assert response.status_code == 200
    return response.json()["otp"]


def test_otp_request_and_password_reset(client: TestClient, user_a: dict) -> None:
    otp = _request_otp(client, USER_A["mobile"])
    new_password = "N3w@Password"

    response = client.post(
        "/user/forgot_password",
        json={
            "mobile": USER_A["mobile"],
            "otp": otp,
            "password": new_password,
            "confirm_password": new_password,
        },
    )

    assert response.status_code == 200
    assert response.json()["message"] == "Password updated successfully"

    # The new password works and the old one does not.
    login(client, USER_A["email"], new_password)
    assert (
        client.post(
            "/user/login_user",
            json={"email": USER_A["email"], "password": USER_A["password"]},
        ).status_code
        == 401
    )


def test_otp_request_for_unknown_mobile_gives_the_same_response(client: TestClient) -> None:
    """Must not become a user-enumeration oracle."""
    response = client.post("/user/forgot_password/request_otp", json={"mobile": "9999999999"})

    assert response.status_code == 200
    assert response.json()["otp"] is None
    assert "If the mobile number is registered" in response.json()["message"]


def test_reset_with_wrong_otp_is_400(client: TestClient, user_a: dict) -> None:
    _request_otp(client, USER_A["mobile"])

    response = client.post(
        "/user/forgot_password",
        json={
            "mobile": USER_A["mobile"],
            "otp": "000000",
            "password": "N3w@Password",
            "confirm_password": "N3w@Password",
        },
    )

    assert response.status_code == 400
    assert response.json()["message"] == "Invalid or expired OTP"


def test_reset_with_expired_otp_is_400(client: TestClient, user_a: dict, db: Session) -> None:
    otp = _request_otp(client, USER_A["mobile"])

    record = db.query(PasswordResetOTP).one()
    record.expires_at = utc_now() - timedelta(seconds=1)
    db.commit()

    response = client.post(
        "/user/forgot_password",
        json={
            "mobile": USER_A["mobile"],
            "otp": otp,
            "password": "N3w@Password",
            "confirm_password": "N3w@Password",
        },
    )

    assert response.status_code == 400


def test_otp_is_single_use(client: TestClient, user_a: dict) -> None:
    otp = _request_otp(client, USER_A["mobile"])
    body = {
        "mobile": USER_A["mobile"],
        "otp": otp,
        "password": "N3w@Password",
        "confirm_password": "N3w@Password",
    }

    assert client.post("/user/forgot_password", json=body).status_code == 200
    assert client.post("/user/forgot_password", json=body).status_code == 400


def test_repeated_wrong_otp_locks_the_code_out(client: TestClient, user_a: dict) -> None:
    otp = _request_otp(client, USER_A["mobile"])
    wrong = {
        "mobile": USER_A["mobile"],
        "otp": "000000" if otp != "000000" else "111111",
        "password": "N3w@Password",
        "confirm_password": "N3w@Password",
    }

    for _ in range(5):
        assert client.post("/user/forgot_password", json=wrong).status_code == 400

    # The sixth attempt is refused outright, and the correct code no longer works.
    locked = client.post("/user/forgot_password", json=wrong)
    assert locked.status_code == 400
    assert "Too many incorrect attempts" in locked.json()["message"]

    correct = client.post("/user/forgot_password", json={**wrong, "otp": otp})
    assert correct.status_code == 400


def test_requesting_a_second_otp_supersedes_the_first(client: TestClient, user_a: dict) -> None:
    first = _request_otp(client, USER_A["mobile"])
    second = _request_otp(client, USER_A["mobile"])
    assert first != second

    stale = client.post(
        "/user/forgot_password",
        json={
            "mobile": USER_A["mobile"],
            "otp": first,
            "password": "N3w@Password",
            "confirm_password": "N3w@Password",
        },
    )
    assert stale.status_code == 400

    fresh = client.post(
        "/user/forgot_password",
        json={
            "mobile": USER_A["mobile"],
            "otp": second,
            "password": "N3w@Password",
            "confirm_password": "N3w@Password",
        },
    )
    assert fresh.status_code == 200


def test_reset_with_mismatched_passwords_is_400(client: TestClient, user_a: dict) -> None:
    otp = _request_otp(client, USER_A["mobile"])

    response = client.post(
        "/user/forgot_password",
        json={
            "mobile": USER_A["mobile"],
            "otp": otp,
            "password": "N3w@Password",
            "confirm_password": "Different@1",
        },
    )

    assert response.status_code == 400
    assert response.json()["message"] == "Password and confirm password do not match"


def test_reset_for_unregistered_mobile_is_404(client: TestClient) -> None:
    response = client.post(
        "/user/forgot_password",
        json={
            "mobile": "9999999999",
            "otp": "123456",
            "password": "N3w@Password",
            "confirm_password": "N3w@Password",
        },
    )

    assert response.status_code == 404
    assert response.json()["message"] == "Mobile number not found"


def test_password_reset_revokes_existing_sessions(client: TestClient, user_a: dict) -> None:
    otp = _request_otp(client, USER_A["mobile"])
    client.post(
        "/user/forgot_password",
        json={
            "mobile": USER_A["mobile"],
            "otp": otp,
            "password": "N3w@Password",
            "confirm_password": "N3w@Password",
        },
    )

    response = client.post("/user/refresh_token", json={"refresh_token": user_a["refresh_token"]})

    assert response.status_code == 401


def test_fetch_user_requires_authentication(client: TestClient, user_a: dict) -> None:
    assert client.get(f"/user/fetch_user_by_id?user_id={user_a['user_id']}").status_code == 401
    assert (
        client.get(
            f"/user/fetch_user_by_id?user_id={user_a['user_id']}", headers=bearer("garbage")
        ).status_code
        == 401
    )
