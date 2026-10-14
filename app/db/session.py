"""Engine, session factory, and the `get_db` request dependency."""

import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.base import Base

logger = logging.getLogger(__name__)

settings = get_settings()

_connect_args: dict[str, Any] = {}
if settings.is_sqlite:
    # FastAPI serves requests from a threadpool; SQLite's default thread check
    # would reject the connection being reused across them.
    _connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    """SQLite ignores FK constraints unless asked, per connection.

    Without this, `ondelete="CASCADE"` on expenses is silently a no-op.
    """
    if not settings.is_sqlite:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_db() -> Generator[Session, None, None]:
    """Yields a session and always closes it, rolling back on an unhandled error."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create any missing tables.

    Adequate for a single-datastore SQLite build. Alembic is the next step before
    this schema changes in a deployed environment.
    """
    import app.models  # noqa: F401  - registers every model on Base.metadata

    Base.metadata.create_all(bind=engine)
    logger.info("Database schema ready at %s", settings.database_url)
