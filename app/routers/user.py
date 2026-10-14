"""/user endpoints (SRS 5.1-5.4, plus refresh, logout and OTP issuance)."""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.config import get_settings
from app.dependencies import CurrentUser, DbSession
from app.models.user import User, UserStatus
from app.schemas.common import FailureResponse, MessageResponse, PaginatedResponse
from app.schemas.user import (
    ForgotPasswordOTPRequest,
    ForgotPasswordRequest,
    LoginResponse,
    OTPRequestResponse,
    RefreshTokenRequest,
    RegisterResponse,
    TokenRefreshResponse,
    UserAdminOut,
    UserDeactivateRequest,
    UserFetchAllRequest,
    UserLoginRequest,
    UserOut,
    UserRegisterRequest,
)
from app.services import auth_service, user_service

router = APIRouter(prefix="/user", tags=["user"])

_AUTH_RESPONSES = {
    401: {"model": FailureResponse, "description": "Missing, expired, or invalid token"},
}


def _to_user_out(user: User) -> UserOut:
    return UserOut(user_id=user.id, name=user.name, mobile=user.mobile, email=user.email)


def _to_user_admin_out(user: User) -> UserAdminOut:
    return UserAdminOut(
        user_id=user.id,
        name=user.name,
        mobile=user.mobile,
        email=user.email,
        user_type=user.user_type,
        status=user.status,
    )


@router.post(
    "/register_user",
    response_model=RegisterResponse,
    summary="Register a new user, or update an existing one",
    responses={
        200: {"model": RegisterResponse, "description": "Existing user's record updated"},
        201: {"model": RegisterResponse, "description": "New user registered"},
        400: {"model": FailureResponse, "description": "Passwords do not match"},
        409: {"model": FailureResponse, "description": "Mobile number already registered to a different user"},
        422: {"model": FailureResponse, "description": "Field validation failure"},
    },
)
def register_user(payload: UserRegisterRequest, db: DbSession, response: Response) -> RegisterResponse:
    """Existing accounts are matched by email (SRS 5.1).

    A match updates the record in place instead of creating a duplicate;
    `password`, `confirm_password` and `email` are immutable on that path and
    are silently ignored rather than applied.
    """
    result = user_service.register_user(db, payload)
    if result.created:
        response.status_code = status.HTTP_201_CREATED
        message = "User registered successfully"
    else:
        response.status_code = status.HTTP_200_OK
        message = "User updated successfully"
    return RegisterResponse(message=message, user_id=result.user.id)


@router.post(
    "/login_user",
    response_model=LoginResponse,
    summary="Authenticate and issue tokens",
    responses={
        401: {"model": FailureResponse, "description": "Invalid credentials"},
        403: {"model": FailureResponse, "description": "Account is inactive"},
        422: {"model": FailureResponse, "description": "Malformed input"},
    },
)
def login_user(payload: UserLoginRequest, db: DbSession) -> LoginResponse:
    user = auth_service.authenticate(db, payload.email, payload.password)
    tokens = auth_service.issue_token_pair(db, user)
    return LoginResponse(
        message="Login successful",
        # `_to_user_admin_out`, not `_to_user_out`: the frontend needs its own
        # `user_type` to decide whether to show the (admin-only) User
        # Management tab. See `LoginResponse.user`'s docstring.
        user=_to_user_admin_out(user),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post(
    "/refresh_token",
    response_model=TokenRefreshResponse,
    summary="Exchange a refresh token for a new token pair",
    responses={401: {"model": FailureResponse, "description": "Invalid or revoked refresh token"}},
)
def refresh_token(payload: RefreshTokenRequest, db: DbSession) -> TokenRefreshResponse:
    tokens = auth_service.rotate_refresh_token(db, payload.refresh_token)
    return TokenRefreshResponse(
        message="Token refreshed successfully",
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Revoke a refresh token",
    responses=_AUTH_RESPONSES,
)
def logout(
    payload: RefreshTokenRequest, db: DbSession, current_user: CurrentUser
) -> MessageResponse:
    auth_service.revoke_refresh_token(db, payload.refresh_token)
    return MessageResponse(message="Logged out successfully")


@router.post(
    "/forgot_password/request_otp",
    response_model=OTPRequestResponse,
    summary="Send a password-reset OTP to a registered mobile number",
)
def request_password_reset_otp(
    payload: ForgotPasswordOTPRequest, db: DbSession
) -> OTPRequestResponse:
    """Always reports success.

    Confirming whether a number is registered would turn this into a user
    enumeration oracle, so the response is identical either way.
    """
    settings = get_settings()
    otp = auth_service.request_password_reset_otp(db, payload.mobile)
    return OTPRequestResponse(
        message="If the mobile number is registered, an OTP has been sent",
        otp=otp if settings.expose_otp_in_response else None,
    )


@router.post(
    "/forgot_password",
    response_model=MessageResponse,
    summary="Reset a password using a verified OTP",
    responses={
        400: {"model": FailureResponse, "description": "Passwords do not match, or invalid OTP"},
        404: {"model": FailureResponse, "description": "Mobile number not registered"},
    },
)
def forgot_password(payload: ForgotPasswordRequest, db: DbSession) -> MessageResponse:
    auth_service.reset_password(
        db,
        mobile=payload.mobile,
        otp=payload.otp,
        password=payload.password,
        confirm_password=payload.confirm_password,
    )
    return MessageResponse(message="Password updated successfully")


@router.get(
    "/fetch_user_by_id",
    response_model=UserOut,
    summary="Fetch a user profile",
    responses={
        **_AUTH_RESPONSES,
        403: {"model": FailureResponse, "description": "Requesting another user's record"},
        404: {"model": FailureResponse, "description": "User not found"},
    },
)
def fetch_user_by_id(
    db: DbSession,
    current_user: CurrentUser,
    user_id: Annotated[int, Query(ge=1, description="Must be the authenticated user's own id")],
) -> UserOut:
    user = user_service.fetch_user_for_requester(db, user_id, current_user)
    return _to_user_out(user)


@router.post(
    "/fetch_all_users",
    response_model=PaginatedResponse[UserAdminOut],
    summary="List all users, sorted and paginated",
    responses={
        **_AUTH_RESPONSES,
        422: {"model": FailureResponse, "description": "Invalid sort field, page, or page size"},
    },
)
def fetch_all_users(
    payload: UserFetchAllRequest, db: DbSession, current_user: CurrentUser
) -> PaginatedResponse[UserAdminOut]:
    """The criteria travel in the request body, so this is a POST despite reading."""
    result = user_service.list_users(
        db,
        sort_by=payload.sort_by,
        sort_order=payload.sort_order,
        page=payload.page,
        page_size=payload.page_size,
        search=payload.search,
    )
    return PaginatedResponse[UserAdminOut](
        page=result.page,
        page_size=result.page_size,
        total_records=result.total_records,
        total_pages=result.total_pages,
        data=[_to_user_admin_out(user) for user in result.items],
    )


@router.post(
    "/deactivate_user",
    response_model=MessageResponse,
    summary="Activate or deactivate a user account (admin only)",
    responses={
        **_AUTH_RESPONSES,
        403: {"model": FailureResponse, "description": "Requester is not an admin"},
        404: {"model": FailureResponse, "description": "User not found"},
        422: {"model": FailureResponse, "description": "status is not 'active' or 'inactive'"},
    },
)
def deactivate_user(
    payload: UserDeactivateRequest, db: DbSession, current_user: CurrentUser
) -> MessageResponse:
    """Only an admin user may call this, for any target user id. `status:
    "active"` activates the account; `status: "inactive"` deactivates it. Any
    other value is rejected with 422 before this handler ever runs."""
    user_service.set_user_status(db, payload.user_id, payload.status, current_user)
    message = (
        "User activated successfully"
        if payload.status is UserStatus.ACTIVE
        else "User deactivated successfully"
    )
    return MessageResponse(message=message)
