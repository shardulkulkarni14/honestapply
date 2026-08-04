"""Cover letter stage: generate and render a cover letter for every TAILORED job.

For each Job with status == Status.TAILORED (respecting limit):
  1. Load the tailored resume from the PDF path's parent (find the matching YAML),
     or fall back to the best matched base resume.
  2. Optionally fetch a company research snippet from <company>/about.
  3. Build the cover letter prompt from prompts/cover_letter.md.
  4. Generate body via get_provider().complete(...).
  5. Render PDF via render_cover_letter_pdf.
  6. Save cover_letter_path, set Status.COVERED.

Returns: count of cover letters successfully generated.
"""

from __future__ import annotations

import re
from pathlib import Path

from honestapply.config import PATHS, get_settings
from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.llm.base import LLMError, get_provider
from honestapply.llm.untrusted import fence
from honestapply.logging_setup import get_logger
from honestapply.resume.renderer import render_cover_letter_pdf
from honestapply.resume.schema import Resume, list_resumes, keyword_match_score

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# --- Language-fabrication guard -------------------------------------------
# Rank of a candidate's ACTUAL proficiency (parsed from resume_facts.languages).
_LEVEL_RANK = {
    "a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6,
    "native": 7, "bilingual": 7, "fluent": 5, "basic": 1,
    "intermediate": 3, "advanced": 5, "proficient": 5,
}
# Rank a CLAIM in the letter implies. `fluent`=5 so "fluent English" is fine for C1.
# Includes German phrasings: muttersprachlich/Muttersprache=native, verhandlungssicher
# (business-fluent)≈C1, fließend=fluent.
_CLAIM_RANK = {
    "native": 7, "bilingual": 7, "mother tongue": 7, "mother-tongue": 7,
    "muttersprache": 7, "muttersprachlich": 7, "native speaker": 7,
    "c2": 6, "c1": 5, "fluent": 5, "fluency": 5, "professional fluency": 5,
    "fließend": 5, "fliessend": 5, "verhandlungssicher": 5, "b2": 4,
}
# Language-name aliases so "German" claims are also caught when phrased "Deutsch".
_LANG_ALIASES = {
    "german": ["german", "deutsch"],
    "english": ["english", "englisch"],
    "french": ["french", "französisch", "francais"],
    "spanish": ["spanish", "spanisch", "español", "espanol"],
}


def _load_prompt(name: str, **kwargs) -> str:
    template = (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return template.format(**kwargs)


def _rank_from_tokens(tokens: list[str]) -> int | None:
    for tok in tokens:
        if tok in _LEVEL_RANK:
            return _LEVEL_RANK[tok]
    return None


def _parse_languages(resume: Resume) -> dict[str, int]:
    """{'english': 5, 'german': 2, ...} from entries like 'German (A2, improving)'."""
    out: dict[str, int] = {}
    for entry in resume.resume_facts.languages or []:
        m = re.match(r"\s*([A-Za-z ]+?)\s*\(([^)]*)\)", entry)
        if not m:
            continue
        lang = m.group(1).strip().lower()
        rank = _rank_from_tokens(re.split(r"[\s,]+", m.group(2).lower()))
        if lang and rank is not None:
            out[lang] = rank
    return out


def _actual_levels(resume: Resume) -> dict[str, int]:
    """Canonical per-language rank: the profile's CEFR map is the cap; the résumé
    list can only lower it, never raise it. Profile is the single source of truth."""
    levels = dict(_parse_languages(resume))
    try:
        from honestapply.config import load_profile
        for lang, lvl in (load_profile().language_levels or {}).items():
            rank = _rank_from_tokens(re.split(r"[\s,]+", str(lvl).strip().lower()))
            if rank is None:
                continue
            key = lang.strip().lower()
            levels[key] = min(levels[key], rank) if key in levels else rank
    except Exception:  # noqa: BLE001 - profile is best-effort; resume list still applies
        pass
    return levels


_RANK_LABEL = {1: "A1", 2: "A2", 3: "B1", 4: "B2", 5: "C1", 6: "C2", 7: "native"}


def _languages_constraint(resume: Resume) -> str:
    """Human-readable hard ceiling for the prompt, e.g.
    'English: up to C2; German: up to A2 — NEVER claim higher (no native/fluent/C1).'"""
    levels = _actual_levels(resume)
    if not levels:
        # fall back to the raw résumé list if nothing parsed
        return ", ".join(resume.resume_facts.languages) or "(none listed)"
    parts = [f"{lang.title()}: up to {_RANK_LABEL.get(rank, '?')}" for lang, rank in sorted(levels.items())]
    return (
        "; ".join(parts)
        + " — NEVER state or imply any language ability above these exact levels "
        "(do not write 'native', 'fluent', 'C1', 'verhandlungssicher', "
        "'muttersprachlich' for a language listed below that level)."
    )


# --- Model-preamble guard --------------------------------------------------
# The prompt ends with "Return only the body of the cover letter", but providers
# ignore that often enough to matter: an observed draft opened with the model
# explaining its own honesty constraints ("Every string in `resume_facts` is
# immutable ... Here's the draft:") followed by a `---` rule. That text rendered
# straight into the PDF that would have been sent to the employer. Strip it in
# code — a prompt instruction is not a guarantee.

# A markdown horizontal rule on its own line, used by models to fence the draft.
_HRULE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)

# Phrases that only ever appear in the model talking *about* the letter.
_META_MARKERS = re.compile(
    r"(here'?s the (?:draft|letter|cover letter)|here is the (?:draft|letter|cover letter)|"
    r"resume_facts|substring-check|substring check|tailor validator|ground truth|"
    r"i'?m treating|i am treating|as requested|let me know if|hope this helps|"
    r"i'?ve (?:written|drafted)|i have (?:written|drafted)|below is the)",
    re.IGNORECASE,
)

# The model narrating the writing TASK rather than addressing the employer.
# Generalises better than the fixed phrase list above, which missed a draft
# opening "I'll draft I need to reason: candidate summary + JD + honesty
# constraints, then produce only the letter body."
_TASK_META = re.compile(
    r"(i'?ll (?:draft|write|produce)|i will (?:draft|write|produce)|"
    r"i need to reason|let me (?:draft|write)|honesty constraints?|"
    r"letter body|candidate summary|per the (?:instructions|prompt)|"
    r"as instructed|following the constraints|only the body)",
    re.IGNORECASE,
)

# How a real letter body opens. This is the RELIABLE signal: blocklisting the
# model's commentary phrases does not generalise — a draft once opened with
# "I'll draft I need to reason: candidate summary + JD + honesty constraints,
# then produce only the letter body." which matched no known marker and shipped
# into the PDF. Anchoring on the letter's own opening catches any preamble,
# whatever it happens to say.
_LETTER_OPENING = re.compile(
    r"(^|\n)[ \t]*("
    r"dear\b|to whom it may concern|hello\b|hi\b|"
    r"i'?m writing|i am writing|i'?d like to apply|i am applying|"
    r"when i (?:read|saw)|your (?:posting|advert)|as (?:a|an) [a-z ]{3,30},"
    r")",
    re.IGNORECASE,
)


def strip_model_preamble(body: str) -> str:
    """Drop any commentary the model wrapped around the letter itself.

    Splits on horizontal rules and keeps the chunk that actually reads like a
    letter, then trims stray leading/trailing meta lines. Deliberately
    conservative: if nothing looks like commentary, the text is returned
    unchanged rather than risking a truncated letter.
    """
    text = (body or "").strip()
    if not text:
        return text

    # Primary rule: if the letter's own opening appears after some other text,
    # everything before it is preamble — regardless of what that preamble says.
    # This is what catches novel commentary that no phrase list anticipates.
    opening = _LETTER_OPENING.search(text)
    if opening and opening.start() > 0:
        head = text[: opening.start()]
        # Only strip when the head really is commentary. A length heuristic is
        # NOT safe here: plenty of good letters open with a hook paragraph and
        # say "I'm writing to apply" further down, and cutting that would delete
        # the strongest part of the letter.
        if _META_MARKERS.search(head) or _TASK_META.search(head) or _HRULE.search(head):
            text = text[opening.start():].lstrip()

    chunks = [c.strip() for c in _HRULE.split(text)]
    chunks = [c for c in chunks if c]
    if len(chunks) > 1:
        # Prefer the longest chunk that opens like a letter; fall back to the
        # longest chunk with no meta markers; else the longest chunk overall.
        letters = [c for c in chunks if _LETTER_OPENING.search(c)]
        clean = [c for c in chunks if not _META_MARKERS.search(c)]
        pool = letters or clean or chunks
        text = max(pool, key=len)

    # Trim leading lines that are pure commentary (the letter hasn't started).
    lines = text.split("\n")
    while lines:
        head = lines[0].strip()
        if head and _META_MARKERS.search(head) and not _LETTER_OPENING.search(head):
            lines.pop(0)
            continue
        break

    # Trim trailing commentary after the sign-off.
    while lines:
        tail = lines[-1].strip()
        if tail and _META_MARKERS.search(tail):
            lines.pop()
            continue
        break

    return "\n".join(lines).strip()


def validate_cover_letter(body: str, resume: Resume) -> list[str]:
    """Return a list of language over-claims (empty == clean).

    Flags the letter asserting a proficiency higher than the candidate's actual
    level for any language (e.g. 'native German' / 'verhandlungssicheres Deutsch'
    when German is A2). Levels come from the profile CEFR map, capped by the
    résumé list, and matching covers English + German name/claim phrasings.
    """
    text = (body or "").lower()
    issues: list[str] = []
    for lang, actual in _actual_levels(resume).items():
        names = _LANG_ALIASES.get(lang, [lang])
        name_alt = "|".join(re.escape(n) for n in names)
        for claim, crank in _CLAIM_RANK.items():
            if crank <= actual:
                continue
            # Allow German adjectival inflection ("verhandlungssicher" →
            # "verhandlungssicheres") for single-word claims; keep tight
            # boundaries for short CEFR codes like "c1"/"b2".
            if claim.isalpha() and len(claim) >= 5:
                cpat = rf"{re.escape(claim)}\w*"
            else:
                cpat = rf"{re.escape(claim)}\b"
            near = (
                rf"\b{cpat}[\w\s,()/-]{{0,15}}\b(?:{name_alt})\b"
                rf"|\b(?:{name_alt})\b[\w\s,()/-]{{0,15}}\b{cpat}"
            )
            if re.search(near, text):
                issues.append(
                    f"letter claims '{claim}' for {lang}, but candidate's level is lower"
                )
                break  # one flag per language is enough
    return issues


def _fetch_company_research(company: str) -> str:
    """Try to fetch a brief snippet from the company's About page.
    Returns empty string on any error — must never block the pipeline."""
    if not company:
        return ""
    try:
        import requests

        # Normalise to a simple slug for URL guessing
        slug = company.lower().strip().replace(" ", "").replace(",", "").replace(".", "")
        url = f"https://www.{slug}.com/about"
        resp = requests.get(url, timeout=4, headers={"User-Agent": "Mozilla/5.0"})
        if resp.ok:
            # Grab first 500 chars of text
            text = resp.text.replace("\n", " ")
            import re
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:500]
    except Exception:
        pass
    return ""


def _get_base_resume_for_job(job: Job, resumes: list[Resume]) -> Resume | None:
    """Return the matched base resume or pick best by keyword score."""
    # Try to reload from matched_resume_path
    if job.matched_resume_path:
        path = Path(job.matched_resume_path)
        if path.exists() and path.suffix in {".yaml", ".yml"}:
            try:
                from honestapply.resume.schema import load_resume
                return load_resume(path)
            except Exception:
                pass

    if not resumes:
        return None

    jd_text = (job.title or "") + "\n\n" + (job.description or "")
    scored = sorted(resumes, key=lambda r: keyword_match_score(r, jd_text), reverse=True)
    return scored[0]


def _cover_one(job: Job, provider, resumes: list[Resume]) -> None:
    resume = _get_base_resume_for_job(job, resumes)
    if resume is None:
        job.status = Status.NEEDS_HUMAN
        job.status_reason = "no resume available for cover letter"
        return

    contact = resume.resume_facts.contact.model_dump()
    candidate_name = contact.get("name", "")
    resume_summary = (
        resume.summary_variants[0] if resume.summary_variants else ""
    )
    candidate_languages = _languages_constraint(resume)

    company_research = _fetch_company_research(job.company or "")

    prompt = _load_prompt(
        "cover_letter",
        candidate_name=candidate_name,
        company=job.company or "",
        role=job.title or "",
        jd_text=fence(
            job.description or "", label="JOB_POSTING", description="job posting"
        ),
        resume_summary=resume_summary,
        candidate_languages=candidate_languages,
        company_research=fence(
            company_research or "",
            label="COMPANY_RESEARCH",
            description="company research summary",
        ),
    )

    # Generate, then guard against fabricated language/credential claims.
    body = strip_model_preamble(provider.complete(prompt))
    issues = validate_cover_letter(body, resume)
    if issues:
        logger.warning("cover: job %d fabrication detected %s — retrying", job.id, issues)
        retry_prompt = (
            prompt
            + "\n\n## CRITICAL CORRECTION\n"
            + "Your previous draft was REJECTED for false claims: "
            + "; ".join(issues)
            + ".\nRewrite the letter making NO claim about any language beyond exactly: "
            + candidate_languages
            + ". Do not state or imply native/fluent/C1+ ability the candidate lacks."
        )
        body = strip_model_preamble(provider.complete(retry_prompt))
        issues = validate_cover_letter(body, resume)
        if issues:
            job.status = Status.NEEDS_HUMAN
            job.status_reason = "cover letter fabricated facts after retry: " + "; ".join(issues)
            logger.error("cover: job %d still fabricating after retry -> needs_human", job.id)
            return

    output_dir = PATHS.job_output_dir(job.id)
    pdf_path = render_cover_letter_pdf(
        body,
        contact,
        output_dir / "cover_letter.pdf",
        company=job.company or "",
        role=job.title or "",
    )

    job.cover_letter_path = str(pdf_path)
    job.status = Status.COVERED
    job.status_reason = None
    logger.info("cover: job %d -> COVERED, PDF at %s", job.id, pdf_path)


def run_cover_letters(limit: int | None = None, ids: list[int] | None = None) -> int:
    """Generate cover letters for TAILORED jobs. Returns count generated.

    *ids* restricts to a curated subset (still TAILORED-only).
    """
    settings = get_settings()
    provider = get_provider(settings)

    resumes = list_resumes(PATHS.resumes_dir)
    logger.info("cover: loaded %d base resumes", len(resumes))

    processed = 0

    with session_scope() as session:
        query = session.query(Job).filter(Job.status == Status.TAILORED)
        if ids:
            query = query.filter(Job.id.in_(ids))
        if limit is not None:
            query = query.limit(limit)
        jobs = query.all()
        logger.info("cover: found %d TAILORED jobs", len(jobs))

        for job in jobs:
            try:
                _cover_one(job, provider, resumes)
                if job.status == Status.COVERED:
                    processed += 1
            except LLMError as exc:
                logger.error("cover: job %d LLM error: %s", job.id, exc)
                job.status = Status.FAILED
                job.status_reason = str(exc)
            except Exception as exc:
                logger.exception("cover: job %d unexpected error: %s", job.id, exc)
                job.status = Status.FAILED
                job.status_reason = str(exc)

    return processed
