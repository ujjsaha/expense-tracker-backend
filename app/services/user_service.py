"""User business logic (SRS 5.1, 5.2, 5.8)."""

import logging
import math
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.security import hash_password
from app.models.user import User, UserStatus, UserType
from app.schemas.common import SortOrder
from app.schemas.user import UserRegisterRequest, UserSortField

logger = logging.getLogger(__name__)

PASSWORD_MISMATCH_MESSAGE = "Password and confirm password do not match"

_SORT_COLUMNS = {
    UserSortField.NAME: User.name,
    UserSortField.EMAIL: User.email,
    UserSortField.MOBILE: User.mobile,
}


@dataclass(frozen=True)
class UserPage:
    items: list[User]
    total_records: int
    total_pages: int
    page: int
    page_size: int


@dataclass(frozen=True)
class UpsertResult:
    user: User
    created: bool


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.lower()))


def get_user_by_mobile(db: Session, mobile: str) -> User | None:
    return db.scalar(select(User).where(User.mobile == mobile))


def assert_passwords_match(password: str, confirm_password: str) -> None:
    """SRS 5.1/5.4 require 400 here, which is why this is not a pydantic validator."""
    if password != confirm_password:
        raise BadRequestError(PASSWORD_MISMATCH_MESSAGE)


def _create_user(db: Session, payload: UserRegisterRequest, email: str) -> User:
    assert_passwords_match(payload.password, payload.confirm_password)

    if get_user_by_mobile(db, payload.mobile) is not None:
        raise ConflictError("Mobile number already registered")

    user = User(
        name=payload.name,
        mobile=payload.mobile,
        email=email,
        password_hash=hash_password(payload.password),
        user_type=payload.user_type,
        status=payload.status,
    )
    db.add(user)

    try:
        db.commit()
    except IntegrityError as exc:
        # Two concurrent registrations can both pass the checks above; the unique
        # constraint is the real arbiter.
        db.rollback()
        raise ConflictError("Email or mobile number already registered") from exc

    db.refresh(user)
    logger.info("Registered user id=%s", user.id)
    return user


def _update_user(db: Session, user: User, payload: UserRegisterRequest) -> User:
    """`password`, `confirm_password` and `email` are immutable once a user
    exists (SRS 5.1). Any values supplied for them are silently ignored -
    neither applied nor validated against each other - rather than rejected.
    """
    if payload.mobile != user.mobile and get_user_by_mobile(db, payload.mobile) is not None:
        raise ConflictError("Mobile number already registered")

    user.name = payload.name
    user.mobile = payload.mobile
    user.user_type = payload.user_type
    user.status = payload.status

    try:
        db.commit()
    except IntegrityError as exc:
        # Mirrors the create-path race: a concurrent request can claim the
        # same mobile number between the check above and this commit.
        db.rollback()
        raise ConflictError("Mobile number already registered") from exc

    db.refresh(user)
    logger.info("Updated user id=%s", user.id)
    return user


def register_user(db: Session, payload: UserRegisterRequest) -> UpsertResult:
    """SRS 5.1: creates a new account, or updates an existing one in place.

    Existence is decided by `email` - the one field, alongside `password` and
    `confirm_password`, that never changes once a user exists. `mobile` is not
    used for the lookup because it is one of the fields this endpoint allows to
    be updated.
    """
    email = payload.email.lower()
    existing = get_user_by_email(db, email)

    if existing is None:
        return UpsertResult(user=_create_user(db, payload, email), created=True)

    return UpsertResult(user=_update_user(db, existing, payload), created=False)


def fetch_user_for_requester(db: Session, user_id: int, requester: User) -> User:
    """SRS 4.4: a user may only read their own record.

    Existence is checked before ownership because the SRS 5.2 table asks for both
    404 (no such user) and 403 (someone else's record) as distinct outcomes. The
    trade-off is that the pair of status codes tells an authenticated caller
    which user ids exist; checking ownership first would collapse both to 403.
    """
    user = get_user_by_id(db, user_id)
    if user is None:
        raise NotFoundError("User not found")

    if user.id != requester.id:
        raise ForbiddenError("You are not permitted to access another user's record")

    return user


def set_user_status(db: Session, user_id: int, status: UserStatus, requester: User) -> User:
    """SRS 5.9: sets the target user's status to the value given in the request
    (`active` activates, `inactive` deactivates). `status` is already confined to
    a `UserStatus` member by the schema, so no other value can reach here.

    Only an admin may call this - a non-admin requester is rejected with 403
    regardless of whose account is targeted, before existence is even checked.
    Idempotent - setting a user to the status it already has still succeeds.
    """
    if requester.user_type is not UserType.ADMIN:
        raise ForbiddenError("Only admin users can activate or deactivate accounts")

    user = get_user_by_id(db, user_id)
    if user is None:
        raise NotFoundError("User not found")

    user.status = status
    db.commit()
    db.refresh(user)
    logger.info("Set status=%s for user id=%s", status.value, user.id)
    return user


def list_users(
    db: Session,
    sort_by: UserSortField,
    sort_order: SortOrder,
    page: int,
    page_size: int,
    search: str | None = None,
) -> UserPage:
    """SRS 5.8: every registered user, sorted and paginated in SQL (SRS 7).

    `search`, if given, is matched case-insensitively and partially against
    name, mobile, email, user_type, and status (OR across all five) - a blank
    or absent search leaves the result unfiltered.
    """
    query = select(User)

    search = (search or "").strip()
    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(
                User.name.ilike(pattern),
                User.mobile.ilike(pattern),
                User.email.ilike(pattern),
                User.user_type.ilike(pattern),
                User.status.ilike(pattern),
            )
        )

    total_records = db.scalar(select(func.count()).select_from(query.subquery())) or 0

    column = _SORT_COLUMNS[sort_by]
    ordering = column.asc() if sort_order is SortOrder.ASC else column.desc()
    # Tiebreaker keeps paging stable when the sort key repeats.
    tiebreak = User.id.asc() if sort_order is SortOrder.ASC else User.id.desc()

    items = list(
        db.scalars(
            query.order_by(ordering, tiebreak).limit(page_size).offset((page - 1) * page_size)
        ).all()
    )

    return UserPage(
        items=items,
        total_records=total_records,
        total_pages=math.ceil(total_records / page_size) if total_records else 0,
        page=page,
        page_size=page_size,
    )
