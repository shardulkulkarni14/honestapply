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

A base résumé with ``tailoring: select`` is a MASTER superset instead: the LLM
picks the bullets / projects / skill items that fit the job and drops the rest.
``validate_selection`` then checks that everything kept is a verbatim copy of
the master, nothing was added or moved between entries, and no non-optional
entry was dropped.

Also exposes:
    validate_immutable_facts(resume, tailored_yaml_text) -> list[str]
        Returns list of missing fact strings (empty = OK).
    validate_selection(resume, tailored_data) -> list[str]
        Returns list of problems with a select-mode output (empty = OK).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from sqlalchemy import select

from honestapply.config import PATHS, get_settings
from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.llm.base import LLMProvider, get_provider
from honestapply.llm.untrusted import fence
from honestapply.logging_setup import get_logger
from honestapply.resume.renderer import render_resume_pdf
from honestapply.resume.schema import (
    Resume,
    iter_immutable_strings,
    keyword_match_score,
    list_resumes,
)
from honestapply.stages._workers import run_pool, run_stage_on_job

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
TAILORED_YAML_NAME = "resume.tailored.yaml"


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


# ---------------------------------------------------------------------------
# Select mode ("tailoring: select"): the base is a master superset
# ---------------------------------------------------------------------------
def _norm_fact(text: str) -> str:
    """Whitespace-collapsed, ``**bold**``-marker-free form used to compare a
    kept string with the master (the renderer treats ``**`` as markup only)."""
    return re.sub(r"\s+", " ", str(text).replace("**", "")).strip()


def _strings_of(value) -> list[str]:
    return [_norm_fact(x) for x in iter_immutable_strings({"_": value})]


def _is_optional(item) -> bool:
    extra = getattr(item, "model_extra", None) or {}
    return bool(extra.get("optional"))


def _skill_items(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [_norm_fact(x) for x in value if str(x).strip()]
    return [_norm_fact(x) for x in str(value).split(",") if x.strip()]


def validate_selection(resume: Resume, tailored_data: dict) -> list[str]:
    """Problems with a select-mode output (empty list = accepted).

    Contact, education and languages must match the master exactly. Every
    experience entry / project that is not ``optional: true`` must be present
    (same company, title, dates, location / name, description) with at least
    one bullet; kept bullets must be verbatim bullets of *that same* entry.
    Skills groups and items, awards and certifications must be subsets of the
    master. Anything else is reported, never silently accepted.
    """
    problems: list[str] = []
    base = resume.resume_facts
    facts = tailored_data.get("resume_facts") if isinstance(tailored_data, dict) else None
    if not isinstance(facts, dict):
        return ["output has no resume_facts mapping"]

    for section in ("contact", "education", "languages"):
        want = _strings_of(getattr(base, section))
        got = _strings_of(facts.get(section))
        for miss in sorted(set(want) - set(got)):
            problems.append(f"{section}: missing {miss!r}")
        for extra in sorted(set(got) - set(want)):
            problems.append(f"{section}: not in master: {extra!r}")

    def _check_entries(section: str, base_items, out_items, key_fields, label_fn):
        if out_items is None:
            out_items = []
        if not isinstance(out_items, list):
            problems.append(f"{section}: expected a list")
            return
        base_by_key = {}
        for item in base_items:
            k = tuple(_norm_fact(getattr(item, f, "")) for f in key_fields)
            base_by_key[k] = item
        seen: set[tuple] = set()
        for raw in out_items:
            if not isinstance(raw, dict):
                problems.append(f"{section}: entry is not a mapping: {raw!r}")
                continue
            k = tuple(_norm_fact(raw.get(f, "") or "") for f in key_fields)
            label = label_fn(raw)
            item = base_by_key.get(k)
            if item is None:
                problems.append(f"{section}: entry not in master: {label}")
                continue
            if k in seen:
                problems.append(f"{section}: entry duplicated: {label}")
                continue
            seen.add(k)
            # every scalar field of the kept entry must equal the master's
            for f, v in raw.items():
                if f in ("bullets", "optional") or v is None:
                    continue
                if _norm_fact(v) != _norm_fact(getattr(item, f, "") or ""):
                    problems.append(f"{section}: {label}: field {f!r} differs from master")
            allowed = {_norm_fact(b) for b in getattr(item, "bullets", [])}
            kept = [b for b in (raw.get("bullets") or []) if str(b).strip()]
            if allowed and not kept:
                problems.append(f"{section}: {label}: no bullets kept")
            for b in kept:
                if _norm_fact(b) not in allowed:
                    problems.append(
                        f"{section}: {label}: bullet not in master entry: {_norm_fact(b)[:90]!r}"
                    )
        for k, item in base_by_key.items():
            if k not in seen and not _is_optional(item):
                problems.append(f"{section}: required entry dropped: {' / '.join(k)}")

    _check_entries(
        "experience", base.experience, facts.get("experience"),
        ("company", "title", "dates"),
        lambda r: f"{r.get('company', '')} / {r.get('title', '')}",
    )
    _check_entries(
        "projects", base.projects, facts.get("projects"),
        ("name",),
        lambda r: str(r.get("name", "")),
    )

    out_skills = facts.get("skills")
    if out_skills is None:
        out_skills = {}
    if not isinstance(out_skills, dict):
        problems.append("skills: expected a mapping")
    else:
        for group, value in out_skills.items():
            if group not in base.skills:
                problems.append(f"skills: group not in master: {group!r}")
                continue
            allowed = set(_skill_items(base.skills[group]))
            kept = _skill_items(value)
            if not kept:
                problems.append(f"skills: {group!r}: empty group kept")
            for it in kept:
                if it not in allowed:
                    problems.append(f"skills: {group!r}: item not in master: {it!r}")

    for section in ("awards", "certifications"):
        allowed = set(_strings_of(getattr(base, section)))
        for s_ in _strings_of(facts.get(section)):
            if s_ not in allowed:
                problems.append(f"{section}: not in master: {s_!r}")
    return problems


def _check_selection(resume: Resume, raw: str) -> tuple[dict, list[str]]:
    """Parse a select-mode LLM reply and validate it; (data, problems)."""
    try:
        data = _parse_yaml_from_llm(raw)
    except yaml.YAMLError as exc:
        return {}, [f"invalid YAML: {exc}"]
    if not isinstance(data, dict):
        return {}, ["output is not a YAML mapping"]
    return data, validate_selection(resume, data)


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


# A base résumé whose contact line declares a relocation ("Relocating to
# Hyderabad, India", "Open to relocating to Abu Dhabi, UAE") is a
# location-specific variant. Picking it by keyword overlap alone put an India
# relocation header and the +91 number on a Munich application (2026-10-20).
_RELOCATION_TARGET = re.compile(r"relocat\w*\s+to\s+([^(|·;)]+)", re.IGNORECASE)

# Country names as they appear in résumé headers → the trailing ISO code some
# boards use instead ("München, BY, DE", "Pune, MH, IN").
_COUNTRY_ISO = {
    "germany": "de",
    "deutschland": "de",
    "india": "in",
    "uae": "ae",
    "united arab emirates": "ae",
    "usa": "us",
    "united states": "us",
    "united kingdom": "gb",
    "uk": "gb",
    "netherlands": "nl",
    "switzerland": "ch",
    "austria": "at",
}


def relocation_target(contact_location: str) -> str | None:
    """The place a résumé header says the candidate is relocating to, or None."""
    m = _RELOCATION_TARGET.search(contact_location or "")
    if not m:
        return None
    return m.group(1).strip().strip(",. ") or None


def resume_fits_job_location(resume: Resume, job_location: str) -> bool:
    """False when *resume* is a relocation variant for somewhere the job is not.

    A résumé with no relocation claim fits everywhere. One that claims a
    relocation fits only when the job's location names the target city or
    country (or its ISO code). An empty job location cannot justify a
    relocation claim, so relocation variants are excluded for it.
    """
    target = relocation_target(resume.resume_facts.contact.location)
    if not target:
        return True
    jl = (job_location or "").lower()
    if not jl.strip():
        return False
    for token in (t.strip().lower() for t in re.split(r"[,/]", target)):
        if not token:
            continue
        if token in jl:
            return True
        iso = _COUNTRY_ISO.get(token)
        if iso and re.search(rf",\s*{iso}\b", jl):
            return True
    return False


def _tailor_one(job: Job, provider, resumes: list[Resume]) -> None:
    jd_text = (job.title or "") + "\n\n" + (job.description or "")

    # --- pick best resume ---
    # Location-specific variants (relocation headers) are only eligible for a
    # job in that place; everything else is ranked by keyword overlap.
    eligible = [r for r in resumes if resume_fits_job_location(r, job.location or "")]
    skipped = len(resumes) - len(eligible)
    if skipped:
        logger.info(
            "tailor: job %d — skipped %d relocation-specific résumé variant(s) for location %r",
            job.id, skipped, job.location,
        )
    if not eligible:
        job.status = Status.NEEDS_HUMAN
        job.status_reason = "every base résumé is a relocation variant for a different location"
        logger.warning("tailor: job %d — no location-neutral base résumé", job.id)
        return
    scored = sorted(
        eligible,
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
    jd_fenced = fence(job.description or "", label="JOB_POSTING", description="job posting")

    if best.selects:
        # --- select mode: the master is a superset; the LLM picks a subset ---
        prompt = _load_prompt(
            "tailor_select",
            resume_yaml=resume_yaml,
            jd_title=job.title or "",
            jd_text=jd_fenced,
        )
        raw = provider.complete(prompt)
        tailored_data, problems = _check_selection(best, raw)
        if problems:
            logger.warning(
                "tailor: job %d select attempt rejected (%d problems), retrying",
                job.id, len(problems),
            )
            problem_list = "\n".join(f"  - {m}" for m in problems[:15])
            retry_suffix = (
                f"\n\n## RETRY CORRECTION\n\nYour previous output was REJECTED by the "
                f"validator for these reasons:\n{problem_list}\n\n"
                "Every kept string must be a character-for-character copy from the "
                "master, in the same entry; non-optional entries must stay with at "
                "least one bullet; contact, education and languages must be complete "
                "and unchanged. Return the complete corrected YAML."
            )
            raw = provider.complete(prompt + retry_suffix)
            tailored_data, problems = _check_selection(best, raw)
        if problems:
            reason = "selection rejected after retry: " + "; ".join(problems[:5])
            job.status = Status.NEEDS_HUMAN
            job.status_reason = reason
            logger.error("tailor: job %d -> NEEDS_HUMAN: %s", job.id, reason)
            return
    else:
        immutable_block = _build_immutable_block(best)
        prompt = _load_prompt(
            "tailor",
            immutable_facts=immutable_block,
            resume_yaml=resume_yaml,
            jd_title=job.title or "",
            jd_text=jd_fenced,
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
    output_dir.mkdir(parents=True, exist_ok=True)
    # The selected/tailored facts, as rendered. Provenance attests the PDF
    # against this (not the master) when the base is a select-mode superset.
    (output_dir / TAILORED_YAML_NAME).write_text(
        yaml.dump(
            {
                "name": tailored_resume.name,
                "base": str(base_path),
                "tailoring": best.tailoring,
                "summary": chosen_summary,
                "resume_facts": tailored_resume.resume_facts.model_dump(),
            },
            allow_unicode=True, default_flow_style=False, sort_keys=False,
        ),
        encoding="utf-8",
    )
    pdf_path = render_resume_pdf(
        tailored_resume,
        output_dir / "resume.pdf",
        summary=chosen_summary,
    )

    job.tailored_resume_path = str(pdf_path)
    job.status = Status.TAILORED
    job.status_reason = None
    logger.info("tailor: job %d -> TAILORED, PDF at %s", job.id, pdf_path)


def tailor_job(
    jid: int,
    *,
    provider: LLMProvider,
    resumes: list[Resume],
    mark_failed: bool = True,
) -> str | None:
    """Tailor one SCORED job in its own transaction; returns the resulting
    status (tailored / needs_human / failed), or None if it wasn't SCORED."""
    return run_stage_on_job(
        jid,
        stage="tailor",
        expects=Status.SCORED,
        work=lambda job: _tailor_one(job, provider, resumes),
        mark_failed=mark_failed,
    )


def _retire_capped_employers(session, job_ids: list[int], total_cap: int) -> list[int]:
    """Drop job ids whose employer is already at the lifetime cap, retiring them.

    Retired jobs are set to ``SKIPPED_COMPANY_CAP`` in *session* (the caller
    commits) so the tailor pool never builds resumes/cover letters for roles the
    apply stage would skip anyway. Returns the ids that should still be tailored.
    A non-positive *total_cap* disables the check and keeps every id.
    """
    if total_cap <= 0 or not job_ids:
        return job_ids
    from honestapply.stages.track import company_total_submissions

    kept: list[int] = []
    retired = 0
    for jid in job_ids:
        job = session.get(Job, jid)
        if job and company_total_submissions(session, job.company or "") >= total_cap:
            job.status = Status.SKIPPED_COMPANY_CAP
            job.status_reason = (
                f"Retired before tailor: employer already has {total_cap}+ "
                "submitted applications (lifetime cap)."
            )
            retired += 1
        else:
            kept.append(jid)
    if retired:
        logger.info("tailor: retired %d SCORED job(s) at the lifetime company cap", retired)
    return kept


def _skip_unauthorized_geo(session, job_ids: list[int], work_auth: dict) -> list[int]:
    """Drop job ids whose location is in a country the profile says the user
    cannot work in (confident "not authorised"), retiring them before tailor.

    Conservative by construction (see ``is_confident_unauthorized``): unknown,
    remote, or unsettled countries are always kept. Returns the ids to tailor.
    """
    if not job_ids:
        return job_ids
    from honestapply.work_authorization import is_confident_unauthorized

    kept: list[int] = []
    retired = 0
    for jid in job_ids:
        job = session.get(Job, jid)
        if job and is_confident_unauthorized(work_auth, job.location or ""):
            job.status = Status.SKIPPED_WORK_AUTH
            job.status_reason = (
                f"Retired before tailor: role in {job.location!r} needs work "
                "authorization / sponsorship the profile does not establish."
            )
            retired += 1
        else:
            kept.append(jid)
    if retired:
        logger.info("tailor: retired %d SCORED job(s) in unauthorized countries", retired)
    return kept


def run_tailor(limit: int | None = None, ids: list[int] | None = None,
               workers: int | None = None) -> int:
    """Tailor resumes for SCORED jobs. Returns count successfully tailored.

    *ids* restricts to a curated subset (still SCORED-only). *workers* jobs are
    tailored at once (default ``HONESTAPPLY_PREPARE_WORKERS``); each commits in
    its own transaction.
    """
    settings = get_settings()
    provider = get_provider(settings)

    resumes = list_resumes(PATHS.resumes_dir)
    if not resumes:
        logger.error("tailor: no resumes found in %s", PATHS.resumes_dir)
        return 0

    logger.info("tailor: loaded %d base resumes", len(resumes))

    with session_scope() as session:
        query = select(Job.id).where(Job.status == Status.SCORED).order_by(Job.id)
        if ids:
            query = query.where(Job.id.in_(ids))
        if limit is not None:
            query = query.limit(limit)
        job_ids = list(session.execute(query).scalars())

        # Retire employers already at the per-company lifetime cap BEFORE spending
        # tailor/cover-letter compute on them. The apply stage enforces the same
        # cap at submit time (its backstop stays); doing it here just avoids
        # building resumes and cover letters for roles that would be skipped.
        total_cap = getattr(settings, "honestapply_per_company_total_cap", 0)
        job_ids = _retire_capped_employers(session, job_ids, total_cap)

        # Opt-in: retire roles in countries the profile can't work in before tailor.
        if getattr(settings, "honestapply_skip_unauthorized_countries", False):
            from honestapply.config import load_profile

            job_ids = _skip_unauthorized_geo(session, job_ids, load_profile().work_authorization)
    logger.info("tailor: found %d SCORED jobs", len(job_ids))

    results = run_pool(
        lambda jid: tailor_job(jid, provider=provider, resumes=resumes),
        job_ids,
        workers,
        label="tailor",
    )
    return sum(1 for r in results if r == Status.TAILORED)
