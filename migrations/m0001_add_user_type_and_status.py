"""Add `user_type` and `status` columns to the `users` table.

Both columns are added with a DB-level default and a CHECK constraint in a
single `ALTER TABLE ... ADD COLUMN` statement. SQLite and PostgreSQL both
populate existing rows with the column's default value as part of that same
statement, so the backfill ('member' / 'active', matching the registration
defaults in app/schemas/user.py) needs no separate UPDATE pass.

Safe to run more than once: each column is skipped if it already exists.

Usage (from the project root, with the venv active):
    python -m migrations.m0001_add_user_type_and_status
"""

import logging

from sqlalchemy import inspect, text

from app.db.session import engine

logger = logging.getLogger(__name__)

_STATEMENTS = {
    "user_type": (
        "ALTER TABLE users ADD COLUMN user_type VARCHAR(10) NOT NULL "
        "DEFAULT 'member' CHECK (user_type IN ('admin', 'member'))"
    ),
    "status": (
        "ALTER TABLE users ADD COLUMN status VARCHAR(10) NOT NULL "
        "DEFAULT 'active' CHECK (status IN ('active', 'inactive'))"
    ),
}


def upgrade() -> None:
    inspector = inspect(engine)
    existing_columns = {col["name"] for col in inspector.get_columns("users")}

    with engine.begin() as conn:
        for column, statement in _STATEMENTS.items():
            if column in existing_columns:
                logger.info("Column users.%s already exists, skipping", column)
                continue
            conn.execute(text(statement))
            logger.info("Added users.%s", column)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    upgrade()
