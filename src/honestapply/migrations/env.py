"""Alembic environment — online migrations against the app's own engine.

The app hands us a live SQLAlchemy engine via ``config.attributes['connection']``
(see honestapply.db.migrate), so we reuse it rather than opening a second
connection with different pragmas. Offline mode is supported for completeness but
the app never uses it.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from honestapply.db.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        # SQLite cannot ALTER most things in place; batch mode recreates the
        # table around the change so migrations work on the project's DB.
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)
    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    if hasattr(connectable, "connect"):
        with connectable.connect() as connection:
            _run(connection)
    else:
        # Already a Connection.
        _run(connectable)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
