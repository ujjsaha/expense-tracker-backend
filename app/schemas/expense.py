"""Expense request/response schemas (SRS sections 3.2 and 5.5-5.7)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.core.clock import utc_now
from app.models.expense import ExpenseCategory
from app.schemas.common import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    IsoUtcDateTime,
    MessageResponse,
    MoneyOut,
    SortOrder,
)

# "Not a future date" (SRS 3.2), with a small allowance so a client whose clock
# runs marginally fast is not rejected.
CLOCK_SKEW_ALLOWANCE = timedelta(seconds=60)

ExpenseNameStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]

AmountDecimal = Annotated[
    Decimal,
    # `decimal_places=2` rejects 10.999 outright rather than rounding it.
    Field(gt=0, max_digits=12, decimal_places=2, examples=[1250.50]),
]


class ExpenseSortField(StrEnum):
    DATE_TIME = "date_time"
    AMOUNT = "amount"
    EXPENSE_CATEGORY = "expense_category"
    EXPENSE_NAME = "expense_name"


# --------------------------------------------------------------------------- #
# Date-range bounds (SRS 5.6 - both bounds are inclusive UTC instants)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DateRange:
    """Half-open interval used for querying: `start <= date_time < end_exclusive`."""

    start: datetime
    end_inclusive: datetime
    end_exclusive: datetime


def _as_utc(value: datetime) -> datetime:
    """A naive bound is read as UTC; a zoned one is converted to it."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalise_not_future(value: datetime) -> datetime:
    """Assume UTC when no zone is given, then reject future timestamps."""
    value = _as_utc(value)
    if value > utc_now() + CLOCK_SKEW_ALLOWANCE:
        raise ValueError("date_time cannot be in the future")
    return value


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class ExpenseCreateRequest(BaseModel):
    """`user_id` is absent by design - it comes from the JWT (SRS 3.3)."""

    expense_category: ExpenseCategory = Field(examples=["Food"])
    expense_name: ExpenseNameStr = Field(examples=["Team lunch"])
    amount: AmountDecimal
    date_time: datetime = Field(examples=["2026-08-01T13:45:00Z"])

    @field_validator("date_time")
    @classmethod
    def _validate_date_time(cls, value: datetime) -> datetime:
        return _normalise_not_future(value)


class ExpenseFetchRequest(BaseModel):
    """Body of `POST /expense/fetch_expense` (SRS 5.6).

    Both bounds are inclusive instants in UTC. Unlike the fields on the expense
    itself these may sit in the future - querying a range that has not happened
    yet is a legitimate request, it simply returns nothing.

    `to_date >= from_date` is deliberately *not* checked here: SRS 5.6 makes an
    inverted range a 400 business rule, and a model validator would surface it as
    a 422 instead. The service layer owns that check.
    """

    from_date: datetime = Field(examples=["2026-08-06T00:00:00Z"])
    to_date: datetime = Field(examples=["2026-08-07T05:05:00Z"])
    sort_by: ExpenseSortField = ExpenseSortField.DATE_TIME
    sort_order: SortOrder = SortOrder.DESC
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    page: int = Field(default=1, ge=1)

    @field_validator("from_date", "to_date")
    @classmethod
    def _validate_bound(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @property
    def date_range(self) -> DateRange:
        """`to_date` is inclusive, so the exclusive end sits one tick past it."""
        return DateRange(
            start=self.from_date,
            end_inclusive=self.to_date,
            end_exclusive=self.to_date + timedelta(microseconds=1),
        )


class ExpenseUpdateRequest(BaseModel):
    """Partial update - every field optional, but at least one required."""

    expense_category: ExpenseCategory | None = None
    expense_name: ExpenseNameStr | None = None
    amount: AmountDecimal | None = None
    date_time: datetime | None = None

    @field_validator("date_time")
    @classmethod
    def _validate_date_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _normalise_not_future(value)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "ExpenseUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class ExpenseOut(BaseModel):
    """List-item shape from SRS 5.6."""

    expense_id: int
    expense_category: ExpenseCategory
    expense_name: str
    amount: MoneyOut
    date_time: IsoUtcDateTime


class ExpenseDetailOut(ExpenseOut):
    """SRS 5.7 "full expense object" - adds the owner and audit timestamps."""

    user_id: int
    created_at: IsoUtcDateTime
    updated_at: IsoUtcDateTime


class ExpenseCreatedResponse(MessageResponse):
    """SRS 5.5 puts expense_id at the top level."""

    expense_id: int
