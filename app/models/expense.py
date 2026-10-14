"""Expense ORM model (SRS section 3.2)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now
from app.core.money import from_minor_units
from app.db.base import Base
from app.db.types import UTCDateTime

if TYPE_CHECKING:
    from app.models.user import User


class ExpenseCategory(StrEnum):
    """Fixed category list (SRS 3.2: "restricted to a defined category list").

    SRS 8 leaves open whether categories become user-definable; that would mean a
    `categories` table replacing this enum.
    """

    FOOD = "Food"
    TRAVEL = "Travel"
    SHOPPING = "Shopping"
    GROCERIES = "Groceries"
    BILLS = "Bills"
    RENT = "Rent"
    HEALTH = "Health"
    EDUCATION = "Education"
    ENTERTAINMENT = "Entertainment"
    FRUITS = "Fruits"
    OTHER = "Other"


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        # SRS section 7: date-range queries are indexed on user_id and date_time.
        Index("ix_expenses_user_id_date_time", "user_id", "date_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    expense_category: Mapped[ExpenseCategory] = mapped_column(
        SAEnum(
            ExpenseCategory,
            native_enum=False,
            length=32,
            # Persist "Food", not "FOOD" - the stored value matches the API contract.
            values_callable=lambda enum_cls: [
                member.value for member in enum_cls],
        ),
        nullable=False,
    )
    expense_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Integer paise. See app/core/money.py for why this is not Numeric.
    amount_minor: Mapped[int] = mapped_column(nullable=False)
    date_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="expenses")

    @property
    def amount(self) -> Decimal:
        """Exact 2 dp value for the API layer."""
        return from_minor_units(self.amount_minor)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Expense id={self.id} user_id={self.user_id} amount_minor={self.amount_minor}>"
