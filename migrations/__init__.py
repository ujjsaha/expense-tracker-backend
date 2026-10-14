"""Standalone schema migrations.

This project has no Alembic setup yet (see the comment on `init_db()` in
app/db/session.py) - `Base.metadata.create_all()` is only additive, so it
cannot evolve a table that already exists. Each module here is a one-off,
idempotent script for a single schema change, run by hand with:

    python -m migrations.<module_name>
"""
