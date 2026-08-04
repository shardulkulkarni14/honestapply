"""Outcome analytics computed from the job_events log.

This is the question a job seeker actually needs answered and the one the
commercial trackers fake or omit: not "how many did I send" but "which of them
came back, and what predicts that". Every number here is derived from recorded
transitions — nothing is estimated — so an empty history yields empty analytics
rather than invented ones.

Definitions, stated because they change what the numbers mean:
  - *applied*: the job reached a real APPLIED transition (dry runs don't count).
  - *positive*: it went on to screening, interviewing, or an offer.
  - *responded*: positive OR an explicit rejection — the employer engaged at all.
    A bare applied job and a ghosted one both count as no response.
  - *interview_rate* = positive / applied. *response_rate* = responded / applied.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from honestapply.db.models import Job, JobEvent, Status

_POSITIVE = {Status.SCREENING, Status.INTERVIEWING, Status.OFFER}
_RESPONDED = _POSITIVE | {Status.REJECTED}


@dataclass
class Outcome:
    applied: int = 0
    positive: int = 0  # reached screening/interviewing/offer
    responded: int = 0  # positive or rejected
    days_to_response: list[float] = field(default_factory=list)

    @property
    def interview_rate(self) -> float | None:
        return round(self.positive / self.applied, 3) if self.applied else None

    @property
    def response_rate(self) -> float | None:
        return round(self.responded / self.applied, 3) if self.applied else None

    @property
    def median_days_to_response(self) -> float | None:
        return round(statistics.median(self.days_to_response), 1) if self.days_to_response else None

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "positive": self.positive,
            "responded": self.responded,
            "interview_rate": self.interview_rate,
            "response_rate": self.response_rate,
            "median_days_to_response": self.median_days_to_response,
        }


def _job_outcomes(session: Session):
    """Yield (job, applied_at, first_response_at, reached) per job that applied.

    ``reached`` is the set of statuses the job ever entered.
    """
    events = session.execute(
        select(JobEvent).order_by(JobEvent.job_id, JobEvent.at)
    ).scalars().all()

    by_job: dict[int, list[JobEvent]] = defaultdict(list)
    for e in events:
        by_job[e.job_id].append(e)

    jobs = {j.id: j for j in session.execute(select(Job)).scalars().all()}

    for job_id, evs in by_job.items():
        reached = {e.to_status for e in evs}
        if Status.APPLIED not in reached:
            continue
        applied_at = next((e.at for e in evs if e.to_status == Status.APPLIED), None)
        first_response_at = next(
            (e.at for e in evs if e.to_status in _RESPONDED and e.at), None
        )
        job = jobs.get(job_id)
        if job is not None and applied_at is not None:
            yield job, applied_at, first_response_at, reached


def _accumulate(outcome: Outcome, applied_at, first_response_at, reached) -> None:
    outcome.applied += 1
    if reached & _POSITIVE:
        outcome.positive += 1
    if reached & _RESPONDED:
        outcome.responded += 1
        if first_response_at is not None:
            outcome.days_to_response.append(
                (first_response_at - applied_at).total_seconds() / 86400
            )


def _score_band(score: int | None) -> str:
    if score is None:
        return "unscored"
    if score >= 9:
        return "9-10"
    if score >= 7:
        return "7-8"
    if score >= 5:
        return "5-6"
    return "0-4"


def compute(session: Session) -> dict:
    """The full analytics payload."""
    # Funnel: current standing of every job, plus a derived no-response bucket.
    funnel: dict[str, int] = defaultdict(int)
    for (status,) in session.execute(select(Job.status)):
        funnel[status] += 1

    overall = Outcome()
    by_ats: dict[str, Outcome] = defaultdict(Outcome)
    by_role: dict[str, Outcome] = defaultdict(Outcome)
    by_band: dict[str, Outcome] = defaultdict(Outcome)
    no_response = 0

    for job, applied_at, first_response_at, reached in _job_outcomes(session):
        _accumulate(overall, applied_at, first_response_at, reached)
        _accumulate(by_ats[job.ats_type or "unknown"], applied_at, first_response_at, reached)
        _accumulate(by_role[job.role_family or "unclassified"], applied_at, first_response_at, reached)
        _accumulate(by_band[_score_band(job.score)], applied_at, first_response_at, reached)
        if not (reached & _RESPONDED):
            no_response += 1

    return {
        "funnel": dict(funnel),
        "applied_total": overall.applied,
        "no_response": no_response,
        "overall": overall.as_dict(),
        "by_ats": {k: v.as_dict() for k, v in sorted(by_ats.items())},
        "by_role_family": {k: v.as_dict() for k, v in sorted(by_role.items())},
        "by_score_band": {k: v.as_dict() for k, v in sorted(by_band.items())},
        "recommended_min_score": _recommend_min_score(by_band),
    }


def _recommend_min_score(by_band: dict[str, Outcome]) -> int | None:
    """The lowest score band that still converts, as a data-driven gate hint.

    Returns the floor of the best-performing band with enough data to trust —
    None until there's signal, so it never fabricates a recommendation.
    """
    floors = {"9-10": 9, "7-8": 7, "5-6": 5, "0-4": 0}
    best, best_rate = None, -1.0
    for band, outcome in by_band.items():
        if outcome.applied < 5 or outcome.interview_rate is None:
            continue
        if outcome.interview_rate > best_rate:
            best, best_rate = floors.get(band), outcome.interview_rate
    return best
