"""Score stage: rate job-fit for every ENRICHED job using an LLM.

For each Job with status == Status.ENRICHED:
  - Build prompt from prompts/score.md using the candidate profile summary and JD.
  - Call get_provider().complete_json(...) expecting:
      {"score": int, "reasoning": str, "matched_keywords": [str], "gap_flags": [str]}
  - Store results; if score < min_score -> Status.SKIPPED_LOW_FIT else Status.SCORED.

Returns: count of jobs processed (scored + skipped).
"""

from __future__ import annotations

import json
from pathlib import Path

from honestapply.config import get_settings, load_profile
from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.llm.base import LLMError, get_provider
from honestapply.llm.untrusted import fence
from honestapply.logging_setup import get_logger

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(name: str, **kwargs) -> str:
    """Load a prompt file from the prompts directory and format with kwargs."""
    template = (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return template.format(**kwargs)


def run_score(min_score: int | None = None, limit: int | None = None,
              ids: list[int] | None = None) -> int:
    """Score ENRICHED jobs. Returns count of jobs processed.

    *limit* caps how many are scored in one call and *ids* restricts to a curated
    subset — together these keep an expensive LLM stage from draining the whole
    ENRICHED backlog in a single run.
    """
    settings = get_settings()
    threshold = min_score if min_score is not None else settings.honestapply_min_score
    provider = get_provider(settings)
    profile = load_profile()
    profile_summary = profile.summary_for_scoring()

    processed = 0

    with session_scope() as session:
        query = session.query(Job).filter(Job.status == Status.ENRICHED)
        if ids:
            query = query.filter(Job.id.in_(ids))
        if limit:
            query = query.limit(limit)
        jobs = query.all()
        logger.info("score: found %d ENRICHED jobs to score", len(jobs))

        for job in jobs:
            try:
                _score_one(job, provider, profile_summary, threshold)
                processed += 1
                logger.info(
                    "score: job %d [%s @ %s] -> score=%s status=%s",
                    job.id, job.title, job.company, job.score, job.status,
                )
            except LLMError as exc:
                logger.error("score: job %d LLM error: %s", job.id, exc)
                job.status = Status.FAILED
                job.status_reason = str(exc)
            except Exception as exc:
                logger.exception("score: job %d unexpected error: %s", job.id, exc)
                job.status = Status.FAILED
                job.status_reason = str(exc)

    return processed


def _score_one(job: Job, provider, profile_summary: str, threshold: int) -> None:
    description = (job.description or "") + ("\n\n" + job.requirements if job.requirements else "")

    prompt = _load_prompt(
        "score",
        profile_summary=profile_summary,
        title=job.title or "",
        company=job.company or "",
        location=job.location or "",
        description=fence(
            description, label="JOB_POSTING", description="job posting"
        ),
    )

    result = provider.complete_json(prompt)

    score = int(result.get("score", 0))
    reasoning = str(result.get("reasoning", ""))
    matched_keywords = result.get("matched_keywords", [])
    gap_flags = result.get("gap_flags", [])

    job.score = score
    job.score_reasoning = reasoning
    job.matched_keywords = json.dumps(matched_keywords)
    job.gap_flags = json.dumps(gap_flags)

    # Classify into grouping dimensions while we have the full posting. Cheap and
    # deterministic (see honestapply.taxonomy); only fills fields left unset, so
    # a manual override from the dashboard is never clobbered by a re-score.
    from honestapply.taxonomy import classify

    tax = classify(job.title or "", job.location or "", job.description)
    for field, value in tax.items():
        if getattr(job, field) is None and value is not None:
            setattr(job, field, value)

    if score < threshold:
        job.status = Status.SKIPPED_LOW_FIT
        job.status_reason = f"score {score} < min {threshold}"
    else:
        job.status = Status.SCORED
        job.status_reason = None
