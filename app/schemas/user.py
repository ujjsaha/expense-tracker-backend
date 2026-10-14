"""User request/response schemas (SRS sections 3.1 and 5.1-5.4).

Password fields exist only on request models. No response model here declares
one, so excluding passwords from responses is structural rather than a filter
someone can forget to apply (SRS 3.3).

Note on `confirm_password`: the match check is deliberately NOT a pydantic
validator. A validator failure becomes 422 before the handler runs, but SRS 5.1
and 5.4 require 400 for a mismatch, so the comparison lives in the service layer.
"""

import re
import string
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, EmailStr, Field, StringConstraints

from app.models.user import UserStatus, UserType
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MessageResponse, SortOrder

MIN_PASSWORD_LENGTH = 8
# bcrypt hashes at most 72 bytes; anything longer would be silently truncated.
MAX_PASSWORD_BYTES = 72

_SPECIAL_CHARACTERS = set(string.punctuation)
_WHITESPACE_RUN = re.compile(r"\s+")


def _collapse_whitespace(value: str) -> str:
    if not isinstance(value, str):
        return value
    return _WHITESPACE_RUN.sub(" ", value).strip()


def _validate_password_strength(value: str) -> str:
    """Report every unmet rule at once instead of one per round-trip."""
    unmet: list[str] = []

    if len(value) < MIN_PASSWORD_LENGTH:
        unmet.append(f"at least {MIN_PASSWORD_LENGTH} characters")
    if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
        unmet.append(f"at most {MAX_PASSWORD_BYTES} bytes")
    if not any(char.isupper() for char in value):
        unmet.append("one uppercase letter")
    if not any(char.islower() for char in value):
        unmet.append("one lowercase letter")
    if not any(char.isdigit() for char in value):
        unmet.append("one digit")
    if not any(char in _SPECIAL_CHARACTERS for char in value):
        unmet.append("one special character")

    if unmet:
        raise ValueError("Password must contain " + ", ".join(unmet))
    return value


# 2-50 characters, alphabetic and spaces only (SRS 3.1).
NameStr = Annotated[
    str,
    BeforeValidator(_collapse_whitespace),
    StringConstraints(min_length=2, max_length=50, pattern=r"^[A-Za-z]+(?: [A-Za-z]+)*$"),
]

# Exactly 10 digits, or E.164 with a country code (SRS 3.1).
MobileStr = Annotated[
    str,
    BeforeValidator(lambda v: v.strip() if isinstance(v, str) else v),
    StringConstraints(pattern=r"^(\d{10}|\+[1-9]\d{7,14})$"),
]

# AfterValidator, not Before: the strength checks assume a real `str`, so they run
# once pydantic has confirmed the type.
PasswordStr = Annotated[str, AfterValidator(_validate_password_strength)]


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class UserRegisterRequest(BaseModel):
    name: NameStr = Field(examples=["Priya Sharma"])
    mobile: MobileStr = Field(examples=["9876543210"])
    email: EmailStr = Field(examples=["priya@example.com"])
    password: PasswordStr = Field(examples=["Str0ng@Pass"])
    confirm_password: str = Field(examples=["Str0ng@Pass"])
    user_type: UserType = Field(default=UserType.MEMBER, examples=["member"])
    status: UserStatus = Field(default=UserStatus.ACTIVE, examples=["active"])


class UserLoginRequest(BaseModel):
    email: EmailStr = Field(examples=["priya@example.com"])
    # Intentionally not PasswordStr: the strength rules apply when choosing a
    # password, not when presenting one. Applying them here would leak the policy
    # and reject users whose password predates a rule change.
    password: str = Field(min_length=1, examples=["Str0ng@Pass"])


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class ForgotPasswordOTPRequest(BaseModel):
    mobile: MobileStr = Field(examples=["9876543210"])


class ForgotPasswordRequest(BaseModel):
    mobile: MobileStr = Field(examples=["9876543210"])
    otp: str = Field(min_length=4, max_length=10, examples=["482913"])
    password: PasswordStr = Field(examples=["N3w@Password"])
    confirm_password: str = Field(examples=["N3w@Password"])


class UserSortField(StrEnum):
    NAME = "name"
    EMAIL = "email"
    MOBILE = "mobile"


class UserFetchAllRequest(BaseModel):
    """Body of `POST /user/fetch_all_users` (SRS 5.8)."""

    sort_by: UserSortField = UserSortField.NAME
    sort_order: SortOrder = SortOrder.ASC
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    page: int = Field(default=1, ge=1)
    search: str | None = Field(
        default=None,
        description=(
            "Case-insensitive partial match against name, mobile, email, "
            "user_type, or status. Absent, null, or empty returns every user."
        ),
        examples=["priya"],
    )


class UserDeactivateRequest(BaseModel):
    """Body of `POST /user/deactivate_user` (SRS 5.9).

    `status` selects the outcome: `"active"` activates the user, `"inactive"`
    deactivates them. Anything outside the `UserStatus` enum is rejected with
    422 before the handler runs.
    """

    user_id: int = Field(ge=1, examples=[101])
    status: UserStatus = Field(examples=["inactive"])


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class UserOut(BaseModel):
    """SRS 5.2 response body."""

    user_id: int
    name: str
    mobile: str
    email: EmailStr


class UserAdminOut(UserOut):
    """SRS 5.8 response body - the admin listing also surfaces user_type and status."""

    user_type: UserType
    status: UserStatus


class RegisterResponse(MessageResponse):
    """SRS 5.1 puts user_id at the top level, not inside `data`."""

    user_id: int


class LoginResponse(MessageResponse):
    """SRS 5.3: user details plus the tokens.

    `user` is `UserAdminOut`, not the plain `UserOut` every other endpoint that
    reports on a single account uses - the frontend's User Management tab is
    admin-only, and `user_type` here is what it checks client-side to decide
    whether to show it at all. `fetch_user_by_id` deliberately still returns
    the narrower `UserOut`; nothing there needs the role.
    """

    user: UserAdminOut
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")


class TokenRefreshResponse(MessageResponse):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class OTPRequestResponse(MessageResponse):
    """`otp` is populated only when EXPOSE_OTP_IN_RESPONSE is on (never in production)."""

    otp: str | None = None
