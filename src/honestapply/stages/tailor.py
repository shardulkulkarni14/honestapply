"""Tailor stage: create a JD-tailored resume for every SCORED job.

For each Job with status == Status.SCORED (respecting limit):
  1. Pick the best base resume by keyword_match_score against the JD.
     If no resume matches any keyword -> Status.NEEDS_HUMAN.
  2. Build tailor prompt, call LLM, get back YAML.
  3. Validate: every string from iter_immutable_strings(base.resume_facts) must
     appear as an exact substring in the returned YAML (after normalising whitespace).
     On failure: retry once with stricter instructions; if still failing ->
     Status.NEEDS_HUMAN.
  4. Load the tailored YAML into a Resume.  If resume_facts are broken/missing,
     fall back to the base resume_facts.
  5. Render PDF via render_resume_pdf, save paths, set Status.TAILORED.

Returns: count of jobs successfully tailored.

Also exposes:
    validate_immutable_facts(resume, tailored_yaml_text) -> list[str]
        Returns list of missing fact strings (empty = OK).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from honestapply.config import PATHS, get_settings
from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.llm.base import LLMError, get_provider
from honestapply.llm.untrusted import fence
from honestapply.logging_setup import get_logger
from honestapply.resume.renderer import render_resume_pdf
from honestapply.resume.schema import (
    Resume,
    iter_immutable_strings,
    keyword_match_score,
    list_resumes,
)

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(name: str, **kwargs) -> str:
    template = (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return template.format(**kwargs)


def _normalise(text: str) -> str:
    """Collapse all whitespace sequences to a single space for comparison."""
    return re.sub(r"\s+", " ", text)


def validate_immutable_facts(resume: Resume, tailored_yaml_text: str) -> list[str]:
    """Return list of fact strings missing from tailored_yaml_text (empty = all present)."""
    normalised_output = _normalise(tailored_yaml_text)
    missing: list[str] = []
    for fact in iter_immutable_strings(resume.resume_facts):
        if _normalise(fact) not in normalised_output:
            missing.append(fact)
    return missing


def _build_immutable_block(resume: Resume) -> str:
    lines = ["# IMMUTABLE FACTS — do not change\n"]
    for fact in iter_immutable_strings(resume.resume_facts):
        lines.append(f"- {fact}")
    return "\n".join(lines)


def _get_resume_yaml(resume: Resume) -> str:
    """Serialize resume to YAML text for the prompt."""
    data = resume.model_dump()
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _parse_yaml_from_llm(raw: str) -> dict:
    """Extract YAML from LLM output (may be wrapped in ```yaml fences)."""
    fenced = re.search(r"```(?:yaml)?\s*(.*?)```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    return yaml.safe_load(raw) or {}


def _tailor_one(job: Job, provider, resumes: list[Resume]) -> None:
    jd_text = (job.title or "") + "\n\n" + (job.description or "")

    # --- pick best resume ---
    scored = sorted(
        resumes,
        key=lambda r: keyword_match_score(r, jd_text),
        reverse=True,
    )
    best = scored[0]
    num_matches, _ = keyword_match_score(best, jd_text)

    if num_matches == 0:
        job.status = Status.NEEDS_HUMAN
        job.status_reason = "no resume matched JD keywords"
        logger.warning("tailor: job %d — no resume matched JD keywords", job.id)
        return

    # Store base resume source path if available
    base_path = getattr(best, "_source_path", None) or str(
        next(
            iter(
                p for p in [
                    PATHS.resumes_dir / f"{best.name}.yaml",
                    PATHS.resumes_dir / f"{best.name}.yml",
                ]
                if p.exists()
            ),
            PATHS.resumes_dir / f"{best.name}.yaml",
        )
    )
    job.matched_resume_path = str(base_path)

    resume_yaml = _get_resume_yaml(best)
    immutable_block = _build_immutable_block(best)

    prompt = _load_prompt(
        "tailor",
        immutable_facts=immutable_block,
        resume_yaml=resume_yaml,
        jd_title=job.title or "",
        jd_text=fence(
            job.description or "", label="JOB_POSTING", description="job posting"
        ),
    )

    # --- first LLM call ---
    raw = provider.complete(prompt)
    missing = validate_immutable_facts(best, raw)

    # --- retry once if validation failed ---
    if missing:
        logger.warning(
            "tailor: job %d first attempt missing %d facts, retrying", job.id, len(missing)
        )
        missing_list = "\n".join(f"  - {m}" for m in missing[:10])
        retry_suffix = (
            f"\n\n## RETRY CORRECTION\n\nYour previous output was REJECTED because the "
            f"following immutable facts were not found verbatim in the output:\n{missing_list}\n\n"
            "You MUST include every one of these strings exactly as written. "
            "Return the complete YAML again with all facts present."
        )
        raw = provider.complete(prompt + retry_suffix)
        missing = validate_immutable_facts(best, raw)

    if missing:
        reason = "immutable facts missing after retry: " + "; ".join(missing[:5])
        job.status = Status.NEEDS_HUMAN
        job.status_reason = reason
        logger.error("tailor: job %d -> NEEDS_HUMAN: %s", job.id, reason)
        return

    # --- parse the tailored YAML ---
    try:
        tailored_data = _parse_yaml_from_llm(raw)
    except yaml.YAMLError as exc:
        job.status = Status.NEEDS_HUMAN
        job.status_reason = f"LLM returned invalid YAML: {exc}"
        logger.error("tailor: job %d — YAML parse failed: %s", job.id, exc)
        return

    # --- build tailored Resume (with safety fallback) ---
    try:
        tailored_resume = Resume(**tailored_data)
        # Safety: if LLM dropped resume_facts, reuse base facts
        if not tailored_resume.resume_facts.experience and best.resume_facts.experience:
            logger.warning("tailor: job %d — LLM dropped resume_facts; falling back", job.id)
            tailored_resume.resume_facts = best.resume_facts
    except Exception as exc:
        logger.warning("tailor: job %d — cannot parse LLM YAML (%s), falling back", job.id, exc)
        tailored_resume = Resume(
            name=best.name,
            target_keywords=best.target_keywords,
            resume_facts=best.resume_facts,
            summary_variants=tailored_data.get("summary_variants", best.summary_variants),
        )

    # Pick summary: first variant from tailored (LLM may have rewritten it)
    chosen_summary = (
        tailored_resume.summary_variants[0]
        if tailored_resume.summary_variants
        else (best.summary_variants[0] if best.summary_variants else "")
    )

    # --- render PDF ---
    output_dir = PATHS.job_output_dir(job.id)
    pdf_path = render_resume_pdf(
        tailored_resume,
        output_dir / "resume.pdf",
        summary=chosen_summary,
    )

    job.tailored_resume_path = str(pdf_path)
    job.status = Status.TAILORED
    job.status_reason = None
    logger.info("tailor: job %d -> TAILORED, PDF at %s", job.id, pdf_path)


def run_tailor(limit: int | None = None, ids: list[int] | None = None) -> int:
    """Tailor resumes for SCORED jobs. Returns count successfully tailored.

    *ids* restricts to a curated subset (still SCORED-only).
    """
    settings = get_settings()
    provider = get_provider(settings)

    resumes = list_resumes(PATHS.resumes_dir)
    if not resumes:
        logger.error("tailor: no resumes found in %s", PATHS.resumes_dir)
        return 0

    logger.info("tailor: loaded %d base resumes", len(resumes))

    processed = 0

    with session_scope() as session:
        query = session.query(Job).filter(Job.status == Status.SCORED)
        if ids:
            query = query.filter(Job.id.in_(ids))
        if limit is not None:
            query = query.limit(limit)
        jobs = query.all()
        logger.info("tailor: found %d SCORED jobs", len(jobs))

        for job in jobs:
            try:
                _tailor_one(job, provider, resumes)
                if job.status == Status.TAILORED:
                    processed += 1
            except LLMError as exc:
                logger.error("tailor: job %d LLM error: %s", job.id, exc)
                job.status = Status.FAILED
                job.status_reason = str(exc)
            except Exception as exc:
                logger.exception("tailor: job %d unexpected error: %s", job.id, exc)
                job.status = Status.FAILED
                job.status_reason = str(exc)

    return processed
