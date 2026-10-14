"""Authentication: login, refresh-token rotation, and the password-reset OTP flow."""

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.config import get_settings
from app.core.exceptions import BadRequestError, ForbiddenError, NotFoundError, UnauthorizedError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    generate_otp,
    hash_otp,
    hash_password,
    hash_token,
    verify_otp,
    verify_password,
)
from app.models.auth import PasswordResetOTP, RefreshToken
from app.models.user import User, UserStatus
from app.notifications import sms
from app.services import user_service

logger = logging.getLogger(__name__)

INVALID_CREDENTIALS_MESSAGE = "Invalid email or password"
INACTIVE_ACCOUNT_MESSAGE = "Account is inactive"
INVALID_OTP_MESSAGE = "Invalid or expired OTP"


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


# --------------------------------------------------------------------------- #
# Login and tokens
# --------------------------------------------------------------------------- #


def authenticate(db: Session, email: str, password: str) -> User:
    """SRS 5.3: the failure message must not reveal whether the email exists."""
    user = user_service.get_user_by_email(db, email)

    if user is None:
        # Hash a throwaway value so a missing account takes about as long as a
        # wrong password, and timing does not become an enumeration oracle.
        verify_password(password, "$2b$12$" + "." * 53)
        raise UnauthorizedError(INVALID_CREDENTIALS_MESSAGE)

    if not verify_password(password, user.password_hash):
        raise UnauthorizedError(INVALID_CREDENTIALS_MESSAGE)

    # Checked only after the credentials are confirmed, not before - otherwise
    # an unauthenticated caller could probe account status without knowing the
    # password, which is an enumeration oracle just like the email check above.
    if user.status is UserStatus.INACTIVE:
        raise ForbiddenError(INACTIVE_ACCOUNT_MESSAGE)

    return user


def issue_token_pair(db: Session, user: User) -> TokenPair:
    settings = get_settings()

    access = create_access_token(user.id)
    refresh = create_refresh_token(user.id)

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh.token),
            expires_at=refresh.expires_at,
        )
    )
    db.commit()

    return TokenPair(
        access_token=access.token,
        refresh_token=refresh.token,
        expires_in=settings.access_token_expire_minutes * 60,
    )


def _revoke_all_for_user(db: Session, user_id: int) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )


def rotate_refresh_token(db: Session, raw_token: str) -> TokenPair:
    """Exchange a refresh token for a new pair, invalidating the presented one.

    Replaying an already-revoked token is treated as theft: the whole family is
    revoked, forcing a fresh login.
    """
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)))

    if stored is None:
        raise UnauthorizedError("Invalid refresh token")

    if stored.is_revoked:
        logger.warning(
            "Refresh token replay detected for user id=%s - revoking all sessions",
            stored.user_id,
        )
        _revoke_all_for_user(db, stored.user_id)
        db.commit()
        raise UnauthorizedError("Refresh token has been revoked")

    if stored.is_expired:
        raise UnauthorizedError("Refresh token has expired")

    user = db.get(User, stored.user_id)
    if user is None:
        raise UnauthorizedError("Invalid refresh token")

    stored.revoked_at = utc_now()
    db.add(stored)
    return issue_token_pair(db, user)


def revoke_refresh_token(db: Session, raw_token: str) -> None:
    """Logout. Unknown or already-revoked tokens are a no-op - the caller's
    session is over either way, and reporting the difference leaks state."""
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)))
    if stored is not None and not stored.is_revoked:
        stored.revoked_at = utc_now()
        db.add(stored)
        db.commit()


# --------------------------------------------------------------------------- #
# Password reset OTP
# --------------------------------------------------------------------------- #


def request_password_reset_otp(db: Session, mobile: str) -> str | None:
    """Issue an OTP if the mobile is registered.

    Returns the raw code only so the router can echo it when
    EXPOSE_OTP_IN_RESPONSE is on. The caller must give the same generic response
    either way, so this endpoint cannot be used to test whether a number exists.
    """
    settings = get_settings()
    user = user_service.get_user_by_mobile(db, mobile)

    if user is None:
        logger.info("Password reset requested for an unregistered mobile")
        return None

    # Supersede any outstanding code so only the newest one works.
    db.execute(
        update(PasswordResetOTP)
        .where(
            PasswordResetOTP.user_id == user.id,
            PasswordResetOTP.consumed_at.is_(None),
        )
        .values(consumed_at=utc_now())
    )

    raw_otp = generate_otp()
    db.add(
        PasswordResetOTP(
            user_id=user.id,
            otp_hash=hash_otp(raw_otp),
            expires_at=utc_now() + timedelta(minutes=settings.otp_expire_minutes),
        )
    )
    db.commit()

    sms.send_otp(user.mobile, raw_otp)
    return raw_otp


def reset_password(
    db: Session, mobile: str, otp: str, password: str, confirm_password: str
) -> User:
    """SRS 5.4, extended with OTP verification."""
    user_service.assert_passwords_match(password, confirm_password)

    user = user_service.get_user_by_mobile(db, mobile)
    if user is None:
        raise NotFoundError("Mobile number not found")

    settings = get_settings()

    record = db.scalar(
        select(PasswordResetOTP)
        .where(
            PasswordResetOTP.user_id == user.id,
            PasswordResetOTP.consumed_at.is_(None),
        )
        .order_by(PasswordResetOTP.created_at.desc(), PasswordResetOTP.id.desc())
    )

    if record is None or record.is_expired:
        raise BadRequestError(INVALID_OTP_MESSAGE)

    if record.attempts >= settings.otp_max_attempts:
        # Burn the code rather than leave it open to further guessing.
        record.consumed_at = utc_now()
        db.add(record)
        db.commit()
        raise BadRequestError("Too many incorrect attempts. Request a new OTP.")

    if not verify_otp(otp, record.otp_hash):
        record.attempts += 1
        db.add(record)
        db.commit()
        raise BadRequestError(INVALID_OTP_MESSAGE)

    record.consumed_at = utc_now()
    user.password_hash = hash_password(password)
    db.add_all([record, user])

    # A password change ends every existing session.
    _revoke_all_for_user(db, user.id)
    db.commit()
    db.refresh(user)

    logger.info("Password reset completed for user id=%s", user.id)
    return user
