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
import re
from pathlib import Path

from sqlalchemy import select

from honestapply.config import Settings, get_settings, load_profile
from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.llm.base import LLMProvider, get_provider
from honestapply.llm.untrusted import fence
from honestapply.logging_setup import get_logger
from honestapply.stages._workers import run_pool, run_stage_on_job

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


# --- Salary-stated detector (HONESTAPPLY_SALARY_STATED_BONUS) ---------------
# Deliberately conservative: a posting only counts when a salary-sized amount
# sits next to a currency AND either forms a range or has a pay word nearby.
# Company figures (funding raised, revenue) use the same currency notation, so
# millions/billions and funding/revenue context are excluded explicitly.
_CUR = r"(?:€|\beur(?:os?)?\b|\$|\busd\b|£|\bgbp\b|\bchf\b)"
_THOUSANDS = r"(?:[.,\u00a0 ]\d{3}|\d{3}|\s?k\b)"
_NOT_BIGGER = (
    r"(?:[.,]\d{2})?(?![.,]?\d)"
    r"(?!\s?(?:m\b|mn\b|mio\b|million|bn\b|billion|mrd\b|milliarde))"
)
_AMOUNT = rf"(?<![\d.,])\d{{2,3}}{_THOUSANDS}{_NOT_BIGGER}"
_MONEY = re.compile(rf"{_CUR}\s?{_AMOUNT}|{_AMOUNT}\s?{_CUR}", re.IGNORECASE)
_RANGE = re.compile(
    rf"(?P<c1>{_CUR}\s?)?(?<![\d.,])\d{{2,3}}{_THOUSANDS}?\s?(?P<c2>{_CUR})?"
    rf"\s?(?:-|–|—|\bto\b|\bbis\b)\s?"
    rf"(?P<c3>{_CUR}\s?)?\d{{2,3}}{_THOUSANDS}{_NOT_BIGGER}\s?(?P<c4>{_CUR})?",
    re.IGNORECASE,
)
_PAY_WORD = re.compile(
    r"(salar(?:y|ies)|gehalt|verg[üu]e?tung|compensation|remuneration|entgelt|"
    r"\bbrutto|pay range|base pay|\bote\b|per year|per annum|\bp\.\s?a\.|"
    r"annual(?:ly)?|pro jahr|j[äa]hrlich)",
    re.IGNORECASE,
)
_COMPANY_MONEY = re.compile(
    r"(rais(?:ed|ing)|funding|funded|finanzierung|invest|series [a-e]\b|seed|"
    r"valuation|bewertung|revenue|umsatz|\barr\b|turnover|budget)",
    re.IGNORECASE,
)
_PAY_WINDOW = 80  # chars either side of the amount searched for a pay word
_COMPANY_WINDOW = 40


def states_salary(text: str | None) -> bool:
    """True when the posting text states a salary figure or range."""
    text = text or ""

    def near(pattern: re.Pattern[str], m: re.Match[str], window: int) -> bool:
        return bool(pattern.search(text[max(0, m.start() - window): m.end() + window]))

    for m in _RANGE.finditer(text):
        if near(_COMPANY_MONEY, m, _COMPANY_WINDOW):
            continue
        has_currency = any(m.group(g) for g in ("c1", "c2", "c3", "c4"))
        if has_currency or near(_PAY_WORD, m, _PAY_WINDOW):
            return True
    for m in _MONEY.finditer(text):
        if near(_PAY_WORD, m, _PAY_WINDOW) and not near(_COMPANY_MONEY, m, _COMPANY_WINDOW):
            return True
    return False


def _load_prompt(name: str, **kwargs) -> str:
    """Load a prompt file from the prompts directory and format with kwargs."""
    template = (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return template.format(**kwargs)


def get_score_provider(settings: Settings | None = None) -> LLMProvider:
    """The provider for scoring: ``HONESTAPPLY_SCORE_PROVIDER`` if set (e.g. score
    over the API while tailor/cover keep the key-free CLI), else the default."""
    settings = settings or get_settings()
    return get_provider(settings, provider=settings.honestapply_score_provider or None)


def score_job(
    jid: int,
    *,
    provider: LLMProvider,
    profile_summary: str,
    threshold: int,
    mark_failed: bool = True,
) -> str | None:
    """Score one ENRICHED job in its own transaction; returns the resulting
    status (scored / skipped_low_fit / failed), or None if it wasn't ENRICHED."""

    def work(job: Job) -> None:
        _score_one(job, provider, profile_summary, threshold)
        logger.info(
            "score: job %d [%s @ %s] -> score=%s status=%s",
            job.id, job.title, job.company, job.score, job.status,
        )

    return run_stage_on_job(
        jid, stage="score", expects=Status.ENRICHED, work=work, mark_failed=mark_failed
    )


def run_score(min_score: int | None = None, limit: int | None = None,
              ids: list[int] | None = None, workers: int | None = None) -> int:
    """Score ENRICHED jobs. Returns count of jobs processed (scored + skipped).

    *limit* caps how many are scored in one call and *ids* restricts to a curated
    subset — together these keep an expensive LLM stage from draining the whole
    ENRICHED backlog in a single run. *workers* jobs are scored at once (default
    ``HONESTAPPLY_PREPARE_WORKERS``); each commits in its own transaction.
    """
    settings = get_settings()
    threshold = min_score if min_score is not None else settings.honestapply_min_score
    provider = get_score_provider(settings)
    profile_summary = load_profile().summary_for_scoring()

    with session_scope() as session:
        query = select(Job.id).where(Job.status == Status.ENRICHED).order_by(Job.id)
        if ids:
            query = query.where(Job.id.in_(ids))
        if limit:
            query = query.limit(limit)
        job_ids = list(session.execute(query).scalars())
    logger.info("score: found %d ENRICHED jobs to score", len(job_ids))

    results = run_pool(
        lambda jid: score_job(
            jid, provider=provider, profile_summary=profile_summary, threshold=threshold
        ),
        job_ids,
        workers,
        label="score",
    )
    return sum(1 for r in results if r in (Status.SCORED, Status.SKIPPED_LOW_FIT))


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

    settings = get_settings()
    # The bonus rewards postings that are transparent about pay. It must never
    # lift a score the hard language rule capped: that cap is a disqualifier,
    # not a fit judgement to be traded off against other signals.
    bonus = settings.honestapply_salary_stated_bonus
    language_capped = isinstance(gap_flags, list) and "german_requirement" in gap_flags
    if bonus > 0 and not language_capped and states_salary(description):
        boosted = min(10, score + bonus)
        if boosted > score:
            reasoning = f"{reasoning} (+{boosted - score}: salary stated)".strip()
            score = boosted
    # Per-host gates (HONESTAPPLY_MIN_SCORE_BY_HOST) are resolved here, the one
    # function every caller scores through, so `honestapply score`, the prepare
    # pool and scripts/batch_drive.py cannot disagree about the gate.
    threshold = settings.min_score_for(job.url, threshold)

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
