"""Single source of truth for "now".

Everything in the app stores and compares timezone-aware UTC datetimes. Tests
monkeypatch this one function rather than the stdlib.
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)
