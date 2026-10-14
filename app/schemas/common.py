"""Shared response envelopes and reusable field types (SRS section 6)."""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, Field, PlainSerializer

T = TypeVar("T")

# Shared across every paginated, sortable list endpoint (e.g. SRS 5.6, 5.8).
DEFAULT_PAGE_SIZE = 10
# An upper bound on page_size keeps one request from pulling the whole table.
MAX_PAGE_SIZE = 100


class SortOrder(StrEnum):
    # Upper case on the wire (SRS 5.6); the comparison below is on the member,
    # not the string, so the casing is purely part of the request contract.
    ASC = "ASC"
    DESC = "DESC"


def _iso_z(value: datetime) -> str:
    """Render UTC as `...Z`, matching the SRS examples rather than `+00:00`."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


IsoUtcDateTime = Annotated[
    datetime,
    PlainSerializer(_iso_z, return_type=str, when_used="json"),
]

MoneyOut = Annotated[
    Decimal,
    # The SRS shows `1250.5`, a JSON number. Pydantic would otherwise emit a
    # string. Storage stays exact (integer paise); only the wire format is float.
    PlainSerializer(float, return_type=float, when_used="json"),
]


class MessageResponse(BaseModel):
    """SRS 6.1 envelope with no payload."""

    status: str = "success"
    message: str


class SuccessResponse(BaseModel, Generic[T]):
    """SRS 6.1 envelope."""

    status: str = "success"
    message: str
    data: T | None = None


class FailureResponse(BaseModel):
    """SRS 6.2 envelope. Produced by the handlers in core/exceptions.py."""

    status: str = "failure"
    message: str
    errors: list[Any] = Field(default_factory=list)


class PaginatedResponse(BaseModel, Generic[T]):
    """SRS 5.6 shape - pagination counters sit at the top level, not inside data."""

    status: str = "success"
    page: int
    page_size: int
    total_records: int
    total_pages: int
    data: list[T]
