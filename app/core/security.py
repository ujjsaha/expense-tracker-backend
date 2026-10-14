"""Password hashing, JWT issuance/verification, and OTP generation."""

import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import bcrypt
import jwt

from app.core.clock import utc_now
from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError

logger = logging.getLogger(__name__)

# bcrypt hashes at most 72 bytes and raises past that; schemas enforce the cap so
# a long password can never be silently truncated to a weaker one.
BCRYPT_MAX_PASSWORD_BYTES = 72


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True)
class TokenPayload:
    user_id: int
    token_type: TokenType
    jti: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


def hash_password(password: str) -> str:
    settings = get_settings()
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(f"Password exceeds {BCRYPT_MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed stored hash, or an over-length candidate. Never raise into
        # the login path - that would distinguish accounts from one another.
        return False


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #


def _create_token(user_id: int, token_type: TokenType, lifetime: timedelta) -> IssuedToken:
    settings = get_settings()
    issued_at = utc_now()
    expires_at = issued_at + lifetime
    jti = uuid.uuid4().hex
    payload = {
        "sub": str(user_id),
        "typ": token_type.value,
        "jti": jti,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return IssuedToken(token=token, jti=jti, expires_at=expires_at)


def create_access_token(user_id: int) -> IssuedToken:
    settings = get_settings()
    return _create_token(
        user_id,
        TokenType.ACCESS,
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def create_refresh_token(user_id: int) -> IssuedToken:
    settings = get_settings()
    return _create_token(
        user_id,
        TokenType.REFRESH,
        timedelta(days=settings.refresh_token_expire_days),
    )


def decode_token(token: str, expected_type: TokenType) -> TokenPayload:
    """Verify signature, expiry, structure and token type.

    Every failure mode surfaces as 401 per SRS 4.4 - including presenting a
    refresh token where an access token is required.
    """
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp", "iat", "typ", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid authentication token") from exc

    if claims.get("typ") != expected_type.value:
        raise UnauthorizedError(f"Expected a {expected_type.value} token")

    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError) as exc:
        raise UnauthorizedError("Invalid authentication token") from exc

    return TokenPayload(
        user_id=user_id,
        token_type=expected_type,
        jti=claims["jti"],
        issued_at=datetime.fromtimestamp(claims["iat"], tz=utc_now().tzinfo),
        expires_at=datetime.fromtimestamp(claims["exp"], tz=utc_now().tzinfo),
    )


def hash_token(token: str) -> str:
    """Deterministic digest for refresh-token storage and lookup.

    SHA-256 is appropriate here: the token is a high-entropy JWT, not a guessable
    secret, and the column has to be searchable.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# OTP
# --------------------------------------------------------------------------- #


def generate_otp() -> str:
    """Cryptographically random numeric code, zero-padded to the configured length."""
    settings = get_settings()
    upper_bound = 10**settings.otp_length
    return str(secrets.randbelow(upper_bound)).zfill(settings.otp_length)


def hash_otp(otp: str) -> str:
    """bcrypt, not SHA-256: a six-digit code has only a million possibilities."""
    salt = bcrypt.gensalt(rounds=get_settings().bcrypt_rounds)
    return bcrypt.hashpw(otp.encode("utf-8"), salt).decode("utf-8")


def verify_otp(otp: str, otp_hash: str) -> bool:
    try:
        return bcrypt.checkpw(otp.encode("utf-8"), otp_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
