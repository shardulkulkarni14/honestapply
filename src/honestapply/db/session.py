"""Engine / session management for the SQLite database."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from honestapply.config import get_settings
from honestapply.db import events as _events

# Record every Job status change into job_events, for all sessions.
_events.install()

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None
# Alembic drives migrations through module-level state (`alembic.context`), so
# two threads calling init_db() at once trample each other. Serialise it.
_INIT_LOCK = threading.Lock()

# How many times a commit that lost the SQLite write-lock race is replayed.
# busy_timeout already makes "database is locked" rare; this is the backstop.
_LOCK_RETRIES = 3


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
        settings = get_settings()
        _engine = create_engine(
            _db_url(db_path),
            future=True,
            # The prepare pool runs one thread per worker, each holding its own
            # connection for the length of a stage (the LLM call included), plus
            # the coordinating thread and whatever CLI command is running
            # alongside. `--workers N` can exceed the .env default, so overflow
            # is unbounded — a SQLite connection is essentially free, and an
            # unbounded overflow is far better than starving workers into a
            # 30s `QueuePool limit` timeout mid-stage.
            pool_size=settings.honestapply_prepare_workers + 2,
            max_overflow=-1,
            connect_args={
                # Belt and braces: the DBAPI-level timeout covers the window
                # before the PRAGMA above has been applied on a brand-new
                # connection.
                "timeout": 30,
                # Pooled connections are handed to whichever thread checks them
                # out next; pysqlite's same-thread guard would refuse that.
                "check_same_thread": False,
            },
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

    with _INIT_LOCK:
        engine = get_engine(db_path)
        run_migrations(engine)
    return engine


# ---------------------------------------------------------------------------
# Lock-contention handling
# ---------------------------------------------------------------------------
def is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and "database is locked" in str(exc).lower()


def _lock_backoff(attempt: int) -> None:
    """Jittered sleep between lock retries: 0.2-1.0s, growing per attempt."""
    time.sleep(random.uniform(0.2, 1.0) * attempt)


def _snapshot_pending(session: Session) -> list[tuple[Any, dict[str, Any]]] | None:
    """Column-level changes on persistent objects, so a rolled-back commit can be
    replayed. Returns None when the pending work includes new objects (their
    cascades and the event listener's appends can't be replayed safely) — the
    caller then re-raises instead of retrying."""
    if session.new or session.deleted:
        return None
    pending: list[tuple[Any, dict[str, Any]]] = []
    for obj in session.dirty:
        state = inspect(obj)
        changes: dict[str, Any] = {}
        for prop in state.mapper.column_attrs:
            hist = state.attrs[prop.key].history  # never triggers a load
            if hist.added:
                changes[prop.key] = hist.added[0]
        if changes:
            pending.append((obj, changes))
    return pending


def _replay_pending(session: Session, pending: list[tuple[Any, dict[str, Any]]]) -> None:
    """Re-apply captured changes after a rollback expired the objects. The
    refresh reloads the row first so attribute history (and therefore the
    job_events from/to pair) reflects the real prior value."""
    for obj, changes in pending:
        session.refresh(obj)
        for key, value in changes.items():
            setattr(obj, key, value)


def _commit_with_retry(session: Session) -> None:
    """``session.commit()`` that survives losing the SQLite write-lock race.

    A "database is locked" on commit means the busy timeout expired while
    another writer held the lock. The failed flush has already rolled the
    session back, so a plain retry would commit nothing: capture the pending
    column changes first, replay them after the rollback, and try again with a
    jittered backoff. Only pure updates are replayed; anything involving new
    or deleted rows is re-raised untouched.
    """
    for attempt in range(1, _LOCK_RETRIES + 1):
        pending = _snapshot_pending(session)
        try:
            session.commit()
            return
        except OperationalError as exc:
            if not is_locked_error(exc) or pending is None or attempt == _LOCK_RETRIES:
                raise
            session.rollback()
            _lock_backoff(attempt)
            _replay_pending(session, pending)


@contextmanager
def session_scope(db_path: Path | None = None) -> Iterator[Session]:
    """Transactional session context. Commits on success, rolls back on error."""
    if _SessionFactory is None or db_path is not None:
        get_engine(db_path)
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        _commit_with_retry(session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def commit_if_status(session: Session, job: Any, expects: str) -> bool:
    """Compare-and-set commit for one pipeline stage.

    The prepare stages read a job, spend a long time on an LLM call with no
    lock held, then write the result. With several workers (or several
    processes) sharing the DB, the write must only land if the job is *still*
    where this stage found it — otherwise two workers could both score the
    same job, or a stage could clobber a status a human set meanwhile.

    ``BEGIN IMMEDIATE`` takes SQLite's single write lock up front (honouring
    busy_timeout), the status is re-read under that lock, and the pending
    changes commit inside the same short transaction. Returns True when the
    write landed; False (and the changes discarded) when the job had moved on.
    Losing the lock race is retried with jitter, since pending ORM changes
    survive a failed ``BEGIN``.
    """
    from honestapply.db.models import Job

    # Autoflush must stay off here: ``session.execute`` would otherwise flush the
    # pending UPDATE first, which both opens the transaction before our BEGIN
    # and makes the re-read see our own new status instead of the DB's.
    with session.no_autoflush:
        for attempt in range(1, _LOCK_RETRIES + 1):
            try:
                session.execute(text("BEGIN IMMEDIATE"))
                break
            except OperationalError as exc:
                if not is_locked_error(exc) or attempt == _LOCK_RETRIES:
                    raise
                _lock_backoff(attempt)
        current = session.execute(
            select(Job.status).where(Job.id == job.id)
        ).scalar_one_or_none()
    if current != expects:
        session.rollback()
        return False
    session.commit()
    return True
