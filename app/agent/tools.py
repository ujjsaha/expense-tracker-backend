"""Read-only tools available to the chatbot agent (chatbot spec section 2).

Built fresh per request in `build_tools(db, user)` and closed over that
specific `db`/`user` - no tool accepts a user id as a model-suppliable
argument, so one conversation can never reach another user's data. Each tool
wraps the existing service layer rather than querying the ORM directly, so
business rules (e.g. SQL-side pagination/aggregation) stay in one place.
"""

from datetime import datetime, timedelta

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.user import User
from app.schemas.common import SortOrder
from app.schemas.expense import DateRange, ExpenseSortField
from app.services import expense_service

_MAX_LISTED_EXPENSES = 20


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ExpenseRangeInput(BaseModel):
    from_date: str = Field(
        description="Inclusive start of the range, ISO 8601 UTC, e.g. 2026-08-01T00:00:00Z"
    )
    to_date: str = Field(
        description="Inclusive end of the range, ISO 8601 UTC, e.g. 2026-08-31T23:59:59Z"
    )


def build_tools(db: Session, user: User) -> list[StructuredTool]:
    def get_expense_summary(from_date: str, to_date: str) -> str:
        """Total spend and a short list of expenses for the current user in an
        inclusive UTC date range."""
        end_inclusive = _parse_utc(to_date)
        date_range = DateRange(
            start=_parse_utc(from_date),
            end_inclusive=end_inclusive,
            end_exclusive=end_inclusive + timedelta(microseconds=1),
        )

        total = expense_service.sum_expenses(db, user, date_range)
        page = expense_service.list_expenses(
            db,
            user=user,
            date_range=date_range,
            sort_by=ExpenseSortField.DATE_TIME,
            sort_order=SortOrder.DESC,
            page=1,
            page_size=_MAX_LISTED_EXPENSES,
        )

        if page.total_records == 0:
            return "No expenses found in that range."

        lines = [f"Total: {total} across {page.total_records} expense(s)."]
        lines += [
            f"- {item.date_time.date()} [{item.expense_category.value}] "
            f"{item.expense_name}: {item.amount}"
            for item in page.items
        ]
        if page.total_records > len(page.items):
            lines.append(f"(Showing the {len(page.items)} most recent; more exist.)")
        return "\n".join(lines)

    def get_user_profile() -> str:
        """The current user's own profile - name, email, mobile."""
        return f"Name: {user.name}, email: {user.email}, mobile: {user.mobile}"

    return [
        StructuredTool.from_function(
            func=get_expense_summary,
            name="get_expense_summary",
            description=(
                "Get the current user's total spend and recent expenses within an "
                "inclusive UTC date range."
            ),
            args_schema=ExpenseRangeInput,
        ),
        StructuredTool.from_function(
            func=get_user_profile,
            name="get_user_profile",
            description="Get the current user's own profile (name, email, mobile).",
        ),
    ]
