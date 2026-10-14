"""Expense business logic (SRS 5.5-5.7).

Filtering, sorting, counting and pagination all execute in SQL. SRS section 7 is
explicit that pagination must not happen in application memory.
"""

import logging
import math
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.core.money import from_minor_units, to_minor_units
from app.models.expense import Expense
from app.models.user import User
from app.schemas.expense import (
    DateRange,
    ExpenseCreateRequest,
    ExpenseSortField,
    ExpenseUpdateRequest,
    SortOrder,
)

logger = logging.getLogger(__name__)

# `amount` sorts on the stored integer column; the two orderings are identical.
_SORT_COLUMNS = {
    ExpenseSortField.DATE_TIME: Expense.date_time,
    ExpenseSortField.AMOUNT: Expense.amount_minor,
    ExpenseSortField.EXPENSE_CATEGORY: Expense.expense_category,
    ExpenseSortField.EXPENSE_NAME: Expense.expense_name,
}


@dataclass(frozen=True)
class ExpensePage:
    items: list[Expense]
    total_records: int
    total_pages: int
    page: int
    page_size: int


def create_expense(db: Session, user: User, payload: ExpenseCreateRequest) -> Expense:
    """`user_id` comes from the authenticated user, never the request body (SRS 3.3)."""
    expense = Expense(
        user_id=user.id,
        expense_category=payload.expense_category,
        expense_name=payload.expense_name,
        amount_minor=to_minor_units(payload.amount),
        date_time=payload.date_time,
    )
    db.add(expense)
    db.commit()
    db.refresh(expense)
    logger.info("Created expense id=%s for user id=%s", expense.id, user.id)
    return expense


def _base_query(user_id: int, date_range: DateRange) -> Select:
    """The range is half-open, so the inclusive upper bound is honoured exactly."""
    return select(Expense).where(
        Expense.user_id == user_id,
        Expense.date_time >= date_range.start,
        Expense.date_time < date_range.end_exclusive,
    )


def list_expenses(
    db: Session,
    user: User,
    date_range: DateRange,
    sort_by: ExpenseSortField,
    sort_order: SortOrder,
    page: int,
    page_size: int,
) -> ExpensePage:
    if date_range.end_inclusive < date_range.start:
        # A business rule rather than a schema error, so 400 (SRS 5.6).
        raise BadRequestError("to_date cannot be earlier than from_date")

    query = _base_query(user.id, date_range)

    total_records = db.scalar(select(func.count()).select_from(query.subquery())) or 0

    column = _SORT_COLUMNS[sort_by]
    ordering = column.asc() if sort_order is SortOrder.ASC else column.desc()
    # Tiebreaker keeps paging stable when the sort key repeats.
    tiebreak = Expense.id.asc() if sort_order is SortOrder.ASC else Expense.id.desc()

    items = list(
        db.scalars(
            query.order_by(ordering, tiebreak).limit(page_size).offset((page - 1) * page_size)
        ).all()
    )

    return ExpensePage(
        items=items,
        total_records=total_records,
        total_pages=math.ceil(total_records / page_size) if total_records else 0,
        page=page,
        page_size=page_size,
    )


def sum_expenses(db: Session, user: User, date_range: DateRange) -> Decimal:
    """Total across every matching expense, not just one page (used by the
    chatbot agent - SQL-side aggregation, per section 7's no-in-memory-paging rule)."""
    query = _base_query(user.id, date_range)
    total_minor = db.scalar(select(func.sum(Expense.amount_minor)).select_from(query.subquery()))
    return from_minor_units(total_minor or 0)


def get_expense_for_user(db: Session, expense_id: int, user: User) -> Expense:
    """404 when it does not exist, 403 when it belongs to someone else (SRS 5.7)."""
    expense = db.get(Expense, expense_id)

    if expense is None:
        raise NotFoundError("Expense not found")
    if expense.user_id != user.id:
        raise ForbiddenError("You are not permitted to access this expense")

    return expense


def update_expense(
    db: Session, expense_id: int, user: User, payload: ExpenseUpdateRequest
) -> Expense:
    expense = get_expense_for_user(db, expense_id, user)

    updates = payload.model_dump(exclude_unset=True)
    if "amount" in updates:
        expense.amount_minor = to_minor_units(updates.pop("amount"))
    for field, value in updates.items():
        setattr(expense, field, value)

    db.add(expense)
    db.commit()
    db.refresh(expense)
    logger.info("Updated expense id=%s for user id=%s", expense.id, user.id)
    return expense


def delete_expense(db: Session, expense_id: int, user: User) -> None:
    expense = get_expense_for_user(db, expense_id, user)
    db.delete(expense)
    db.commit()
    logger.info("Deleted expense id=%s for user id=%s", expense_id, user.id)
