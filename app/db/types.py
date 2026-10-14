"""Custom column types.

SQLite stores datetimes without a zone. `UTCDateTime` makes that invisible to the
rest of the app: everything written is normalised to UTC, and everything read
back comes out timezone-aware, so no naive datetime ever reaches business logic
(where it would silently compare wrong against `utc_now()`).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # A naive value reaching here is assumed UTC rather than local time.
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
