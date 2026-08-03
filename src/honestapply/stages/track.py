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
            select(Job.status, func.count(Job.id)).group_by(Job.status)
        ).all()
    return {status: count for status, count in rows}


def summarize(limit: int = 10) -> StatusSummary:
    summary = StatusSummary()
    with session_scope() as s:
        rows = s.execute(
            select(Job.status, func.count(Job.id)).group_by(Job.status)
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
