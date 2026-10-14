"""Authentication, token handling and the refresh-token flow (SRS 4.x, 5.3)."""

from datetime import timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.clock import utc_now
from app.core.config import get_settings
from app.core.security import TokenType
from tests.conftest import USER_A, bearer

PROTECTED_ENDPOINTS = [
    ("get", "/user/fetch_user_by_id?user_id=1"),
    ("post", "/expense/create_expense"),
    ("post", "/expense/fetch_expense"),
    ("get", "/expense/fetch_expense_by_id?expense_id=1"),
    ("put", "/expense/update_expense?expense_id=1"),
    ("delete", "/expense/delete_expense?expense_id=1"),
]


def _make_token(user_id: int, token_type: TokenType, lifetime: timedelta) -> str:
    settings = get_settings()
    now = utc_now()
    return jwt.encode(
        {
            "sub": str(user_id),
            "typ": token_type.value,
            "jti": "test-jti",
            "iat": int(now.timestamp()),
            "exp": int((now + lifetime).timestamp()),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


def test_login_returns_tokens_and_user_details(client: TestClient, user_a: dict) -> None:
    body = client.post(
        "/user/login_user",
        json={"email": USER_A["email"], "password": USER_A["password"]},
    ).json()

    assert body["status"] == "success"
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["email"] == USER_A["email"]
    assert body["user"]["user_id"] == user_a["user_id"]
    # A freshly registered account is a member (SRS 3.1's default) and active
    # (an inactive account cannot reach this point at all - `authenticate`
    # rejects it with 403 first).
    assert body["user"]["user_type"] == "member"
    assert body["user"]["status"] == "active"


def test_login_response_reflects_a_promoted_admin_role(
    client: TestClient, admin_headers: dict[str, str], user_a: dict
) -> None:
    """The frontend's User Management tab is admin-only and reads `user_type`
    off this response to decide whether to show it - it must reflect the
    account's *current* role, not the one it had at registration."""
    body = client.post(
        "/user/login_user",
        json={"email": USER_A["email"], "password": USER_A["password"]},
    ).json()

    assert body["user"]["user_type"] == "admin"


def test_login_with_wrong_password_is_401(client: TestClient, user_a: dict) -> None:
    response = client.post(
        "/user/login_user", json={"email": USER_A["email"], "password": "Wr0ng@Password"}
    )

    assert response.status_code == 401
    assert response.json() == {
        "status": "failure",
        "message": "Invalid email or password",
        "errors": [],
    }


def test_login_failure_message_does_not_reveal_whether_email_exists(
    client: TestClient, user_a: dict
) -> None:
    """SRS 5.3 calls for a deliberately generic message."""
    unknown = client.post(
        "/user/login_user", json={"email": "nobody@example.com", "password": "Wr0ng@Password"}
    )
    wrong_password = client.post(
        "/user/login_user", json={"email": USER_A["email"], "password": "Wr0ng@Password"}
    )

    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json()["message"] == wrong_password.json()["message"]


def test_inactive_user_cannot_log_in(client: TestClient, user_a: dict, db) -> None:
    from app.models.user import User

    db.query(User).filter_by(id=user_a["user_id"]).update({"status": "inactive"})
    db.commit()

    response = client.post(
        "/user/login_user", json={"email": USER_A["email"], "password": USER_A["password"]}
    )

    assert response.status_code == 403
    assert response.json() == {
        "status": "failure",
        "message": "Account is inactive",
        "errors": [],
    }


def test_deactivating_own_account_then_logging_in_is_rejected(
    client: TestClient, user_a: dict, admin_headers: dict
) -> None:
    """End-to-end: an admin deactivates via the API, then confirm login is refused."""
    deactivate = client.post(
        "/user/deactivate_user",
        json={"user_id": user_a["user_id"], "status": "inactive"},
        headers=admin_headers,
    )
    assert deactivate.status_code == 200

    response = client.post(
        "/user/login_user", json={"email": USER_A["email"], "password": USER_A["password"]}
    )

    assert response.status_code == 403
    assert response.json()["message"] == "Account is inactive"


# --------------------------------------------------------------------------- #
# The auth guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("method", "url"), PROTECTED_ENDPOINTS)
def test_missing_authorization_header_is_401_not_403(
    client: TestClient, method: str, url: str
) -> None:
    """FastAPI's stock HTTPBearer answers 403 here; SRS 4.4 requires 401."""
    # `request` rather than `client.get(...)` so a body can be sent uniformly -
    # the guard must fire before any body validation.
    response = client.request(method.upper(), url, json={})

    assert response.status_code == 401
    assert response.json()["status"] == "failure"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_malformed_token_is_401(client: TestClient) -> None:
    response = client.get("/user/fetch_user_by_id?user_id=1", headers=bearer("not-a-jwt"))

    assert response.status_code == 401
    assert response.json()["message"] == "Invalid authentication token"


def test_token_signed_with_another_secret_is_401(client: TestClient, user_a: dict) -> None:
    forged = jwt.encode(
        {"sub": str(user_a["user_id"]), "typ": "access", "jti": "x", "iat": 0, "exp": 9999999999},
        "a-completely-different-secret-of-a-respectable-length-0123456789",
        algorithm="HS256",
    )

    response = client.get("/user/fetch_user_by_id?user_id=1", headers=bearer(forged))

    assert response.status_code == 401


def test_expired_access_token_is_401(client: TestClient, user_a: dict) -> None:
    expired = _make_token(user_a["user_id"], TokenType.ACCESS, timedelta(minutes=-5))

    response = client.get(
        f"/user/fetch_user_by_id?user_id={user_a['user_id']}", headers=bearer(expired)
    )

    assert response.status_code == 401
    assert response.json()["message"] == "Token has expired"


def test_refresh_token_is_rejected_as_an_access_token(client: TestClient, user_a: dict) -> None:
    """A refresh token must not open protected routes."""
    response = client.get(
        f"/user/fetch_user_by_id?user_id={user_a['user_id']}",
        headers=bearer(user_a["refresh_token"]),
    )

    assert response.status_code == 401
    assert response.json()["message"] == "Expected a access token"


def test_token_for_a_deleted_account_is_401(client: TestClient, user_a: dict) -> None:
    ghost = _make_token(999_999, TokenType.ACCESS, timedelta(minutes=5))

    response = client.get("/user/fetch_user_by_id?user_id=999999", headers=bearer(ghost))

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Refresh rotation
# --------------------------------------------------------------------------- #


def test_refresh_issues_a_new_pair_and_invalidates_the_old_token(
    client: TestClient, user_a: dict
) -> None:
    response = client.post("/user/refresh_token", json={"refresh_token": user_a["refresh_token"]})

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["refresh_token"] != user_a["refresh_token"]

    # The new access token works.
    assert (
        client.get(
            f"/user/fetch_user_by_id?user_id={user_a['user_id']}",
            headers=bearer(body["access_token"]),
        ).status_code
        == 200
    )


def test_replaying_a_rotated_refresh_token_revokes_every_session(
    client: TestClient, user_a: dict
) -> None:
    """Reuse of a spent token is treated as theft, not as a retry."""
    first = client.post(
        "/user/refresh_token", json={"refresh_token": user_a["refresh_token"]}
    ).json()

    replay = client.post("/user/refresh_token", json={"refresh_token": user_a["refresh_token"]})
    assert replay.status_code == 401
    assert replay.json()["message"] == "Refresh token has been revoked"

    # The replacement token issued moments ago is revoked too.
    followup = client.post("/user/refresh_token", json={"refresh_token": first["refresh_token"]})
    assert followup.status_code == 401


def test_unknown_refresh_token_is_401(client: TestClient, user_a: dict) -> None:
    response = client.post("/user/refresh_token", json={"refresh_token": "nonsense"})

    assert response.status_code == 401


def test_expired_refresh_token_is_401(client: TestClient, user_a: dict, db) -> None:
    from app.models.auth import RefreshToken

    stored = db.query(RefreshToken).one()
    stored.expires_at = utc_now() - timedelta(seconds=1)
    db.commit()

    response = client.post("/user/refresh_token", json={"refresh_token": user_a["refresh_token"]})

    assert response.status_code == 401
    assert response.json()["message"] == "Refresh token has expired"


# --------------------------------------------------------------------------- #
# Logout
# --------------------------------------------------------------------------- #


def test_logout_revokes_the_refresh_token(
    client: TestClient, user_a: dict, auth_headers: dict
) -> None:
    logout = client.post(
        "/user/logout", json={"refresh_token": user_a["refresh_token"]}, headers=auth_headers
    )
    assert logout.status_code == 200

    reuse = client.post("/user/refresh_token", json={"refresh_token": user_a["refresh_token"]})
    assert reuse.status_code == 401


def test_logout_requires_authentication(client: TestClient, user_a: dict) -> None:
    response = client.post("/user/logout", json={"refresh_token": user_a["refresh_token"]})

    assert response.status_code == 401
