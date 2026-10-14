"""/expense endpoints (SRS 5.5-5.7, plus update and delete)."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.dependencies import CurrentUser, DbSession
from app.models.expense import Expense
from app.schemas.common import FailureResponse, MessageResponse, PaginatedResponse
from app.schemas.expense import (
    ExpenseCreatedResponse,
    ExpenseCreateRequest,
    ExpenseDetailOut,
    ExpenseFetchRequest,
    ExpenseOut,
    ExpenseUpdateRequest,
)
from app.services import expense_service

router = APIRouter(prefix="/expense", tags=["expense"])

_AUTH_RESPONSES = {
    401: {"model": FailureResponse, "description": "Missing, expired, or invalid token"},
}
_OWNERSHIP_RESPONSES = {
    **_AUTH_RESPONSES,
    403: {"model": FailureResponse, "description": "Expense belongs to another user"},
    404: {"model": FailureResponse, "description": "Expense not found"},
}


def _to_expense_out(expense: Expense) -> ExpenseOut:
    return ExpenseOut(
        expense_id=expense.id,
        expense_category=expense.expense_category,
        expense_name=expense.expense_name,
        amount=expense.amount,
        date_time=expense.date_time,
    )


def _to_expense_detail(expense: Expense) -> ExpenseDetailOut:
    return ExpenseDetailOut(
        expense_id=expense.id,
        user_id=expense.user_id,
        expense_category=expense.expense_category,
        expense_name=expense.expense_name,
        amount=expense.amount,
        date_time=expense.date_time,
        created_at=expense.created_at,
        updated_at=expense.updated_at,
    )


@router.post(
    "/create_expense",
    status_code=status.HTTP_201_CREATED,
    response_model=ExpenseCreatedResponse,
    summary="Record a new expense",
    responses={
        **_AUTH_RESPONSES,
        400: {"model": FailureResponse, "description": "Business rule violation"},
        422: {"model": FailureResponse, "description": "Field validation failure"},
    },
)
def create_expense(
    payload: ExpenseCreateRequest, db: DbSession, current_user: CurrentUser
) -> ExpenseCreatedResponse:
    expense = expense_service.create_expense(db, current_user, payload)
    return ExpenseCreatedResponse(message="Expense created successfully", expense_id=expense.id)


@router.post(
    "/fetch_expense",
    response_model=PaginatedResponse[ExpenseOut],
    summary="List expenses in a date range, sorted and paginated",
    responses={
        **_AUTH_RESPONSES,
        400: {"model": FailureResponse, "description": "to_date earlier than from_date"},
        422: {"model": FailureResponse, "description": "Invalid sort field, page, or date format"},
    },
)
def fetch_expense(
    payload: ExpenseFetchRequest, db: DbSession, current_user: CurrentUser
) -> PaginatedResponse[ExpenseOut]:
    """The criteria travel in the request body, so this is a POST despite reading.

    `from_date` and `to_date` are required ISO 8601 datetimes, both inclusive and
    interpreted in UTC. `to_date` is inclusive of exactly the instant given, so a
    caller wanting a whole day should pass `...T23:59:59Z`.

    An empty range is a 200 with an empty `data` array, never a 404 (SRS 5.6).
    """
    result = expense_service.list_expenses(
        db,
        user=current_user,
        date_range=payload.date_range,
        sort_by=payload.sort_by,
        sort_order=payload.sort_order,
        page=payload.page,
        page_size=payload.page_size,
    )
    return PaginatedResponse[ExpenseOut](
        page=result.page,
        page_size=result.page_size,
        total_records=result.total_records,
        total_pages=result.total_pages,
        data=[_to_expense_out(expense) for expense in result.items],
    )


@router.get(
    "/fetch_expense_by_id",
    response_model=ExpenseDetailOut,
    summary="Fetch a single expense",
    responses=_OWNERSHIP_RESPONSES,
)
def fetch_expense_by_id(
    db: DbSession,
    current_user: CurrentUser,
    expense_id: Annotated[int, Query(ge=1)],
) -> ExpenseDetailOut:
    expense = expense_service.get_expense_for_user(db, expense_id, current_user)
    return _to_expense_detail(expense)


@router.put(
    "/update_expense",
    response_model=ExpenseDetailOut,
    summary="Update fields on an existing expense",
    responses={
        **_OWNERSHIP_RESPONSES,
        422: {"model": FailureResponse, "description": "Field validation failure"},
    },
)
def update_expense(
    payload: ExpenseUpdateRequest,
    db: DbSession,
    current_user: CurrentUser,
    expense_id: Annotated[int, Query(ge=1)],
) -> ExpenseDetailOut:
    expense = expense_service.update_expense(db, expense_id, current_user, payload)
    return _to_expense_detail(expense)


@router.delete(
    "/delete_expense",
    response_model=MessageResponse,
    summary="Delete an expense",
    responses=_OWNERSHIP_RESPONSES,
)
def delete_expense(
    db: DbSession,
    current_user: CurrentUser,
    expense_id: Annotated[int, Query(ge=1)],
) -> MessageResponse:
    expense_service.delete_expense(db, expense_id, current_user)
    return MessageResponse(message="Expense deleted successfully")
