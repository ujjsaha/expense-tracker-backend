"""Shared test configuration and fixtures.

The environment is configured before any application module is imported, so
`get_settings()` caches test values rather than whatever is in the developer's
local .env.
"""

import os
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

os.environ["APP_ENV"] = "test"
os.environ["JWT_SECRET_KEY"] = "test-secret-key-long-enough-for-the-length-check-0123456789"
os.environ["DATABASE_URL"] = "sqlite://"
# bcrypt's default cost would make this suite take minutes; 4 is the floor the
# config allows and keeps hashing behaviour identical.
os.environ["BCRYPT_ROUNDS"] = "4"
os.environ["EXPOSE_OTP_IN_RESPONSE"] = "true"
os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"] = "15"
os.environ["OTP_MAX_ATTEMPTS"] = "5"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.expense import Expense, ExpenseCategory

USER_A = {
    "name": "Priya Sharma",
    "mobile": "9876543210",
    "email": "priya@example.com",
    "password": "Str0ng@Pass",
    "confirm_password": "Str0ng@Pass",
}

USER_B = {
    "name": "Rahul Verma",
    "mobile": "9123456780",
    "email": "rahul@example.com",
    "password": "An0ther@Pass",
    "confirm_password": "An0ther@Pass",
}

SEEDED_EXPENSE_COUNT = 25


@pytest.fixture
def engine() -> Generator[Engine, None, None]:
    """One in-memory database per test.

    StaticPool keeps a single connection alive, otherwise each checkout would get
    a fresh - and empty - in-memory database.
    """
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    yield test_engine
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture
def db(session_factory: sessionmaker) -> Generator[Session, None, None]:
    """Direct database access for seeding and for asserting persisted state."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(session_factory: sessionmaker) -> Generator[TestClient, None, None]:
    """TestClient is used without its context manager on purpose.

    Entering it would run the lifespan handler, which calls `init_db()` against
    the application's own engine. Tests drive the overridden session instead.
    """

    def override_get_db() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Account helpers
# --------------------------------------------------------------------------- #


def register(client: TestClient, payload: dict) -> int:
    response = client.post("/user/register_user", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["user_id"]


def login(client: TestClient, email: str, password: str) -> dict:
    response = client.post("/user/login_user", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user_a(client: TestClient) -> dict:
    user_id = register(client, USER_A)
    tokens = login(client, USER_A["email"], USER_A["password"])
    return {
        "user_id": user_id,
        "email": USER_A["email"],
        "password": USER_A["password"],
        "mobile": USER_A["mobile"],
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
    }


@pytest.fixture
def user_b(client: TestClient) -> dict:
    user_id = register(client, USER_B)
    tokens = login(client, USER_B["email"], USER_B["password"])
    return {
        "user_id": user_id,
        "email": USER_B["email"],
        "password": USER_B["password"],
        "mobile": USER_B["mobile"],
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
    }


@pytest.fixture
def auth_headers(user_a: dict) -> dict[str, str]:
    return bearer(user_a["access_token"])


@pytest.fixture
def other_auth_headers(user_b: dict) -> dict[str, str]:
    return bearer(user_b["access_token"])


@pytest.fixture
def admin_headers(user_a: dict, db: Session) -> dict[str, str]:
    """Promotes `user_a` to admin directly in the database.

    There is no registration path for creating an admin - user_type defaults
    to member (SRS 3.1) - so admin-only tests seed the role this way.
    """
    from app.models.user import User, UserType

    user = db.query(User).filter_by(id=user_a["user_id"]).one()
    user.user_type = UserType.ADMIN
    db.commit()
    return bearer(user_a["access_token"])


# --------------------------------------------------------------------------- #
# Expense seeding
# --------------------------------------------------------------------------- #

_CATEGORY_CYCLE = [
    ExpenseCategory.FOOD,
    ExpenseCategory.TRAVEL,
    ExpenseCategory.SHOPPING,
    ExpenseCategory.BILLS,
    ExpenseCategory.HEALTH,
]


@pytest.fixture
def seed_start_date() -> datetime:
    """Anchor the fixture data in the past so `not a future date` never trips."""
    anchor = datetime.now(UTC) - timedelta(days=40)
    return anchor.replace(hour=12, minute=0, second=0, microsecond=0)


@pytest.fixture
def seeded_expenses(db: Session, user_a: dict, seed_start_date: datetime) -> list[dict]:
    """25 expenses on consecutive days, with distinct amounts and cycling categories.

    Inserted through the ORM rather than the API so the dates are exactly
    controlled and the suite stays fast.
    """
    records = []
    for index in range(SEEDED_EXPENSE_COUNT):
        occurred_at = seed_start_date + timedelta(days=index)
        amount = Decimal(f"{100 + index * 7}.{index % 100:02d}")
        expense = Expense(
            user_id=user_a["user_id"],
            expense_category=_CATEGORY_CYCLE[index % len(_CATEGORY_CYCLE)],
            expense_name=f"Expense {index:02d}",
            amount_minor=int(amount * 100),
            date_time=occurred_at,
        )
        db.add(expense)
        records.append(
            {
                "index": index,
                "amount": amount,
                "date_time": occurred_at,
                "expense_name": expense.expense_name,
                "expense_category": expense.expense_category,
            }
        )

    db.commit()

    for record, expense in zip(records, db.query(Expense).order_by(Expense.id).all(), strict=True):
        record["expense_id"] = expense.id

    return records
