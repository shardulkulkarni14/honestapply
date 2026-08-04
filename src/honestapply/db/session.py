"""Engine / session management for the SQLite database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from honestapply.config import get_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _db_url(db_path: Path | None = None) -> str:
    path = db_path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Make the database usable by more than one process at a time.

    Stages are routinely run concurrently — e.g. preparing two markets in
    parallel — and SQLite's defaults make that fail immediately: `journal_mode`
    is `delete` (a writer locks the whole file against readers) and
    `busy_timeout` is 0 (a blocked writer errors instantly rather than waiting).

    WAL lets readers continue while one writer works, and a 30s busy timeout lets
    concurrent writers queue through the short per-job transactions instead of
    raising "database is locked".
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def get_engine(db_path: Path | None = None) -> Engine:
    global _engine, _SessionFactory
    if _engine is None or db_path is not None:
        _engine = create_engine(
            _db_url(db_path),
            future=True,
            # Belt and braces: the DBAPI-level timeout covers the window before
            # the PRAGMA above has been applied on a brand-new connection.
            connect_args={"timeout": 30},
        )
        event.listen(_engine, "connect", _apply_sqlite_pragmas)
        _SessionFactory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    return _engine


def init_db(db_path: Path | None = None) -> Engine:
    """Bring the database schema up to date. Returns the engine.

    Runs Alembic migrations rather than ``create_all`` so that schema changes
    reach databases that already have data — including databases created in the
    create_all era, which are stamped at the baseline and then upgraded. See
    honestapply.db.migrate.
    """
    from honestapply.db.migrate import run_migrations

    engine = get_engine(db_path)
    run_migrations(engine)
    return engine


@contextmanager
def session_scope(db_path: Path | None = None) -> Iterator[Session]:
    """Transactional session context. Commits on success, rolls back on error."""
    if _SessionFactory is None or db_path is not None:
        get_engine(db_path)
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
