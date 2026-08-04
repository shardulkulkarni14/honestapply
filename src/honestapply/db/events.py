"""Automatic status-transition logging.

Every change to a Job's status should leave a timestamped row in job_events —
that is what makes time-to-response and days-in-stage computable. Rather than
trust 23-odd call sites across the stages to each remember to write that row
(they won't, and #2395 in a peer project is a live example of exactly that kind
of rule silently rotting), a single ``before_flush`` listener observes status
changes on the way to the database and records them. Forgetting is impossible
because no stage code is involved.

Who caused the change, and any note, come from :func:`transition` — a context
manager the caller wraps around the write. The default is an unattributed
pipeline step with no note, which is correct for every automated stage.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from honestapply.db.models import Job, JobEvent

# Contextual attribution for whatever status changes happen inside `transition`.
_source: contextvars.ContextVar[str] = contextvars.ContextVar(
    "transition_source", default="pipeline"
)
_note: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "transition_note", default=None
)


@contextmanager
def transition(source: str, note: str | None = None) -> Iterator[None]:
    """Attribute any Job status changes made in this block to ``source``.

    Used by the dashboard (``source="dashboard"``) and inbox sync
    (``source="email"``) so their transitions are distinguishable from pipeline
    steps in analytics, and so a human note can ride along with the change.
    """
    tok_s = _source.set(source)
    tok_n = _note.set(note)
    try:
        yield
    finally:
        _source.reset(tok_s)
        _note.reset(tok_n)


def _new_job_status(obj: Job) -> str | None:
    """The status a just-created Job will have once inserted.

    Column defaults (``default=Status.DISCOVERED``) are applied by SQLAlchemy
    during the INSERT, which happens *after* before_flush — so a Job added
    without an explicit status still reads ``None`` here. Resolve the scalar
    column default ourselves so the first event isn't a NULL to_status.
    """
    if obj.status is not None:
        return obj.status
    default = Job.__table__.c.status.default
    if default is not None and not getattr(default, "is_callable", False):
        return default.arg
    return None


def _record_transitions(session: Session, _flush_context, _instances) -> None:
    source = _source.get()
    note = _note.get()

    # Newly created jobs: their first event is entry into their initial status.
    # Append via the relationship so the cascade fills job_id once the Job is
    # inserted — the id does not exist yet at flush time.
    for obj in session.new:
        if isinstance(obj, Job):
            status = _new_job_status(obj)
            if status is not None:
                obj.events.append(
                    JobEvent(from_status=None, to_status=status, source=source, note=note)
                )

    # Existing jobs whose status column actually changed this flush.
    for obj in session.dirty:
        if not isinstance(obj, Job):
            continue
        hist = inspect(obj).attrs.status.history
        if not hist.has_changes():
            continue
        old = hist.deleted[0] if hist.deleted else None
        new = hist.added[0] if hist.added else obj.status
        if old == new:
            continue
        session.add(
            JobEvent(
                job_id=obj.id,
                from_status=old,
                to_status=new,
                source=source,
                note=note,
            )
        )


def install() -> None:
    """Register the listener once. Idempotent."""
    if not event.contains(Session, "before_flush", _record_transitions):
        event.listen(Session, "before_flush", _record_transitions)
