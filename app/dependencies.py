"""Shared FastAPI dependencies, including the auth guard (SRS section 4.4)."""

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import UnauthorizedError
from app.core.security import TokenType, decode_token
from app.db.session import get_db
from app.models.user import User

# auto_error=False matters: FastAPI's HTTPBearer answers a *missing* Authorization
# header with 403, but SRS 4.4 requires 401 for missing or invalid credentials.
# Turning the built-in error off lets us raise the correct status ourselves.
bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <access_token>")

DbSession = Annotated[Session, Depends(get_db)]
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def get_current_user(credentials: BearerCredentials, db: DbSession) -> User:
    """Resolve the caller from a validated access token.

    Signature, expiry, structure and token type are all checked; every failure is
    a 401. A refresh token presented here is rejected - it is not an access token.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Authentication credentials were not provided")

    payload = decode_token(credentials.credentials, expected_type=TokenType.ACCESS)

    user = db.get(User, payload.user_id)
    if user is None:
        # Token verified but the account is gone - treat as unauthenticated.
        raise UnauthorizedError("Invalid authentication token")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
