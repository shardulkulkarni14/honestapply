"""SQLAlchemy 2.0 ORM models — the single source of truth for pipeline state.

Status lifecycle:
    discovered -> enriched -> scored -> tailored -> covered -> ready_to_apply
                 -> applied | needs_human | failed
    (plus skipped_low_fit off the `score` stage, dry_run_completed off `apply`)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def url_hash(url: str) -> str:
    return hashlib.sha256((url or "").strip().lower().encode()).hexdigest()[:16]


# --- Status constants (kept as plain strings for SQLite friendliness) ------
class Status:
    # Pipeline stages, in order.
    DISCOVERED = "discovered"
    ENRICHED = "enriched"
    SCORED = "scored"
    SKIPPED_LOW_FIT = "skipped_low_fit"
    TAILORED = "tailored"
    COVERED = "covered"
    READY_TO_APPLY = "ready_to_apply"
    APPLIED = "applied"
    DRY_RUN_COMPLETED = "dry_run_completed"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"

    # Post-apply lifecycle. These come *after* a real submission and are set by
    # the user (or an inbox sync), not the pipeline — which is why they used to
    # live in a side JSON file. They are real statuses now so that every
    # transition is timestamped in job_events and analytics can be computed.
    SCREENING = "screening"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    GHOSTED = "ghosted"

    # Stages the pipeline itself drives.
    PIPELINE = [
        DISCOVERED, ENRICHED, SCORED, SKIPPED_LOW_FIT, TAILORED, COVERED,
        READY_TO_APPLY, APPLIED, DRY_RUN_COMPLETED, NEEDS_HUMAN, FAILED,
    ]
    # Outcomes a human (or inbox sync) records after applying.
    POST_APPLY = [SCREENING, INTERVIEWING, OFFER, REJECTED, GHOSTED]

    ALL = [*PIPELINE, *POST_APPLY]


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("company", "title", "url_hash", name="uq_job_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # provenance
    source_board: Mapped[str] = mapped_column(String(64), default="")
    external_id: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    url_hash: Mapped[str] = mapped_column(String(32), index=True, default="")

    # core fields
    company: Mapped[str] = mapped_column(String(255), default="", index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    location: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str | None] = mapped_column(Text, default=None)
    requirements: Mapped[str | None] = mapped_column(Text, default=None)
    salary_range_text: Mapped[str | None] = mapped_column(String(255), default=None)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # pipeline
    status: Mapped[str] = mapped_column(String(32), default=Status.DISCOVERED, index=True)
    ats_type: Mapped[str | None] = mapped_column(String(32), default=None)
    score: Mapped[int | None] = mapped_column(Integer, default=None)
    score_reasoning: Mapped[str | None] = mapped_column(Text, default=None)
    matched_keywords: Mapped[str | None] = mapped_column(Text, default=None)  # JSON list
    gap_flags: Mapped[str | None] = mapped_column(Text, default=None)  # JSON list

    # artifacts
    matched_resume_path: Mapped[str | None] = mapped_column(Text, default=None)
    tailored_resume_path: Mapped[str | None] = mapped_column(Text, default=None)
    cover_letter_path: Mapped[str | None] = mapped_column(Text, default=None)

    # bookkeeping
    status_reason: Mapped[str | None] = mapped_column(Text, default=None)
    # Free-form running note the user keeps on a job — distinct from the
    # per-transition notes on job_events, which are an audit log. This is the
    # scratchpad ("recruiter prefers mornings"); those are the history.
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    applications: Mapped[list["Application"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    events: Mapped[list["JobEvent"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobEvent.at",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Job {self.id} {self.company!r} {self.title!r} [{self.status}]>"


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    mode: Mapped[str] = mapped_column(String(16), default="dry_run")  # dry_run | real
    status: Mapped[str] = mapped_column(String(32), default="")  # applied|needs_human|failed
    confirmation_text: Mapped[str | None] = mapped_column(Text, default=None)
    pre_submit_screenshot: Mapped[str | None] = mapped_column(Text, default=None)
    post_submit_screenshot: Mapped[str | None] = mapped_column(Text, default=None)
    error_log: Mapped[str | None] = mapped_column(Text, default=None)

    job: Mapped["Job"] = relationship(back_populates="applications")


class JobEvent(Base):
    """One row per status transition — the timestamped history of a job.

    A job's ``status`` column holds only where it is *now*; without this table a
    transition leaves no trace, so nothing time-based (time-to-response,
    days-in-stage, funnel velocity) can be computed. Append-only: rows are a log,
    never edited. ``source`` records who moved it — the pipeline, the dashboard,
    an inbox sync — which is what lets analytics separate machine steps from
    human outcomes.
    """

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    # Null only for the very first event, which has no prior state.
    from_status: Mapped[str | None] = mapped_column(String(32), default=None)
    to_status: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(16), default="pipeline")
    note: Mapped[str | None] = mapped_column(Text, default=None)

    job: Mapped["Job"] = relationship(back_populates="events")


class RunLog(Base):
    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stage: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    jobs_processed: Mapped[int] = mapped_column(Integer, default=0)
    jobs_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
