"""Status reporting helpers, shared by the `status` CLI command and dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select

from honestapply.db.models import Application, Job, Status
from honestapply.db.session import session_scope


@dataclass
class StatusSummary:
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0
    recent_applications: list[dict] = field(default_factory=list)
    real_submissions: int = 0
    dry_runs: int = 0
    success_rate: float = 0.0


def status_counts() -> dict[str, int]:
    with session_scope() as s:
        rows = s.execute(
            select(Job.status, func.count(Job.id))
            .where(Job.archived_at.is_(None))  # archived jobs are out of the pipeline
            .group_by(Job.status)
        ).all()
    return {status: count for status, count in rows}


def summarize(limit: int = 10) -> StatusSummary:
    summary = StatusSummary()
    with session_scope() as s:
        rows = s.execute(
            select(Job.status, func.count(Job.id))
            .where(Job.archived_at.is_(None))  # archived jobs are out of the pipeline
            .group_by(Job.status)
        ).all()
        summary.counts = {status: count for status, count in rows}
        summary.total = sum(summary.counts.values())

        apps = (
            s.execute(
                select(Application).order_by(Application.applied_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )
        for a in apps:
            summary.recent_applications.append(
                {
                    "job_id": a.job_id,
                    "applied_at": a.applied_at.isoformat() if a.applied_at else "",
                    "mode": a.mode,
                    "status": a.status,
                    "confirmation": (a.confirmation_text or "")[:80],
                }
            )

        summary.real_submissions = (
            s.execute(
                select(func.count(Application.id)).where(Application.mode == "real")
            ).scalar_one()
        )
        summary.dry_runs = (
            s.execute(
                select(func.count(Application.id)).where(Application.mode == "dry_run")
            ).scalar_one()
        )

    applied = summary.counts.get(Status.APPLIED, 0)
    attempted = applied + summary.counts.get(Status.FAILED, 0)
    summary.success_rate = (applied / attempted) if attempted else 0.0
    return summary


def needs_human_jobs(limit: int | None = None) -> list[dict]:
    """Jobs routed to ``needs_human`` (newest first), with the reason and URL.

    These are forms the apply stage filled but could not submit on its own
    (a question it cannot answer truthfully, a CAPTCHA, an account wall). The
    list lets you finish them by hand. Archived jobs are excluded.
    """
    with session_scope() as s:
        query = (
            select(Job.id, Job.company, Job.title, Job.status_reason, Job.url, Job.updated_at)
            .where(Job.status == Status.NEEDS_HUMAN, Job.archived_at.is_(None))
            .order_by(Job.updated_at.desc().nullslast(), Job.id.desc())
        )
        if limit is not None:
            query = query.limit(limit)
        return [
            {
                "job_id": jid,
                "company": company or "",
                "title": title or "",
                "reason": reason or "",
                "url": url or "",
            }
            for jid, company, title, reason, url, _ in s.execute(query).all()
        ]


def company_total_submissions(session, company: str) -> int:
    """All-time count of DISTINCT roles really applied to at *company*.

    Single source of truth for the per-employer lifetime cap. The apply stage
    enforces the cap at submit time; earlier stages (e.g. tailor) use this same
    counter to retire a capped employer's queued roles *before* spending compute
    on them. Counts DISTINCT ``job_id`` because the apply stage can write more
    than one Application row per job.
    """
    if not company:
        return 0
    return (
        session.execute(
            select(func.count(func.distinct(Application.job_id)))
            .join(Job, Job.id == Application.job_id)
            .where(
                func.lower(func.trim(Job.company)) == company.strip().lower(),
                Application.mode == "real",
                Application.status == "applied",
            )
        ).scalar_one()
    ) or 0


def real_submissions_today() -> int:
    """Count of real submissions in the last 24h — feeds the daily cap check."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    with session_scope() as s:
        return (
            s.execute(
                select(func.count(Application.id)).where(
                    Application.mode == "real", Application.applied_at >= cutoff
                )
            ).scalar_one()
        )
