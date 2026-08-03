"""Stage 6 — Apply: browser-automation orchestrator.

Fills and submits job applications via Claude Code + Playwright MCP.

Two modes:
  MODE A (default): loop over COVERED jobs, fill + submit (or dry-run) each.
  MODE B (--url):   generate the apply_instructions.md for a single URL and print
                    the path; no DB writes, no subprocess spawning.

Entry point (called by CLI):
    run_apply(dry_run, no_safety, limit, url, enable_linkedin_easy_apply)
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

from honestapply.ats.detect import detect_ats
from honestapply.config import (
    HARD_DAILY_CEILING,
    PATHS,
    get_settings,
    load_answers,
    load_ats_selectors,
    load_profile,
)
from honestapply.db.models import Application, Job, Status, url_hash
from honestapply.db.session import session_scope
from honestapply.logging_setup import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE_PATH = Path(__file__).parent.parent / "prompts" / "apply_browser.md"

_RESULT_RE = re.compile(
    r"<<<RESULT>>>\s*(\{.*?\})\s*<<<END>>>",
    re.DOTALL,
)


def _is_mock() -> bool:
    # Read at call time (not import time) so tests / sim that set the env var
    # after importing this module still take the mock path.
    return bool(os.environ.get("HONESTAPPLY_APPLY_MOCK", ""))


def _domain(url: str) -> str:
    return (urlparse(url).hostname or url).lower()


# A submission only "counts" for dedup if it was a real, completed apply.
_REAL_SUBMITTED_STATUSES = ("applied", "submitted")


def _has_real_submission(session, job_id: int) -> bool:
    """True iff *job_id* already has a real, completed Application (not dry-run/needs_human)."""
    return (
        session.query(Application)
        .filter(
            Application.job_id == job_id,
            Application.mode == "real",
            Application.status.in_(_REAL_SUBMITTED_STATUSES),
        )
        .first()
        is not None
    )


def _load_prompt_template() -> str:
    if _PROMPT_TEMPLATE_PATH.exists():
        return _PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt template not found: {_PROMPT_TEMPLATE_PATH}")


def _find_recommendation_pdf() -> str:
    """Return the first recommendation PDF in data/recommendations/, or empty string."""
    rec_dir = PATHS.recommendations_dir
    if rec_dir.exists():
        pdfs = sorted(rec_dir.glob("*.pdf"))
        if pdfs:
            return str(pdfs[0].resolve())
    return ""


def _render_template(template: str, substitutions: dict[str, str]) -> str:
    """Replace `{key}` placeholders in template with values.

    Only replaces keys that are in ``substitutions``; any other curly-brace
    constructs (e.g. JSON fragments, YAML inline dicts) are left untouched by
    using a simple token-by-token approach rather than str.format().
    """
    result = template
    for key, value in substitutions.items():
        result = result.replace("{" + key + "}", value)
    return result


def _build_instructions(
    job_url: str,
    job_id: int | str,
    ats_type: str,
    dry_run: bool,
) -> tuple[str, Path]:
    """Render apply_browser.md with per-job context. Returns (text, output_path)."""
    template = _load_prompt_template()

    job_dir = PATHS.job_output_dir(job_id)

    # Artifact paths
    resume_pdf = job_dir / "resume.pdf"
    cover_letter_pdf = job_dir / "cover_letter.pdf"
    pre_screenshot = job_dir / "pre_submit.png"
    post_screenshot = job_dir / "post_submit.png"
    rec_pdf = _find_recommendation_pdf()

    # Config data
    profile = load_profile()
    answers = load_answers()
    selectors_all = load_ats_selectors()
    ats_selectors = selectors_all.get(ats_type, selectors_all.get("generic", {}))

    profile_json_str = json.dumps(profile.model_dump(), indent=2, ensure_ascii=False)
    answers_yaml_str = yaml.dump(answers, allow_unicode=True, default_flow_style=False)
    ats_selectors_str = yaml.dump(ats_selectors, allow_unicode=True, default_flow_style=False)

    substitutions = {
        "job_url": job_url,
        "ats_type": ats_type,
        "dry_run": str(dry_run),
        "resume_pdf_path": str(resume_pdf.resolve()),
        "cover_letter_pdf_path": str(cover_letter_pdf.resolve()),
        "recommendation_pdf_path": rec_pdf,
        "profile_json": profile_json_str,
        "answers_yaml": answers_yaml_str,
        "ats_selectors": ats_selectors_str,
        "pre_submit_screenshot_path": str(pre_screenshot.resolve()),
        "post_submit_screenshot_path": str(post_screenshot.resolve()),
    }

    instructions = _render_template(template, substitutions)

    out_path = job_dir / "apply_instructions.md"
    out_path.write_text(instructions, encoding="utf-8")
    return instructions, out_path


def _mock_result(dry_run: bool, job_dir: Path) -> dict:
    """Return a synthetic result dict and create placeholder screenshot files."""
    pre = job_dir / "pre_submit.png"
    post = job_dir / "post_submit.png"
    pre.touch()
    if not dry_run:
        post.touch()
    return {
        "status": "dry_run_completed" if dry_run else "applied",
        "reason": "[mock]",
        "confirmation_text": "[mock confirmation]",
        "pre_submit_screenshot": str(pre),
        "post_submit_screenshot": "" if dry_run else str(post),
    }


def _run_claude(instructions_text: str) -> dict:
    """Invoke the claude CLI and parse the <<<RESULT>>> block from stdout."""
    try:
        proc = subprocess.run(
            ["claude", "--dangerously-skip-permissions", "-p", instructions_text],
            capture_output=True,
            text=True,
            timeout=600,
        )
        stdout = proc.stdout or ""
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": "claude subprocess timed out (600s)", "confirmation_text": "", "pre_submit_screenshot": "", "post_submit_screenshot": ""}
    except Exception as exc:
        return {"status": "failed", "reason": f"claude subprocess error: {exc}", "confirmation_text": "", "pre_submit_screenshot": "", "post_submit_screenshot": ""}

    # Parse the LAST <<<RESULT>>>...<<<END>>> block
    matches = _RESULT_RE.findall(stdout)
    if not matches:
        tail = stdout[-2000:] if len(stdout) > 2000 else stdout
        return {"status": "failed", "reason": "No <<<RESULT>>> block found in claude output", "confirmation_text": "", "pre_submit_screenshot": "", "post_submit_screenshot": "", "_error_log": tail}
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        return {"status": "failed", "reason": f"JSON parse error in result block: {exc}", "confirmation_text": "", "pre_submit_screenshot": "", "post_submit_screenshot": "", "_error_log": matches[-1]}


def _persist(
    session,
    job: Job,
    result: dict,
    dry_run: bool,
) -> Application:
    """Create an Application row and update Job.status from result."""
    status_map = {
        "applied": Status.APPLIED,
        "dry_run_completed": Status.DRY_RUN_COMPLETED,
        "needs_human": Status.NEEDS_HUMAN,
        "failed": Status.FAILED,
    }

    result_status = result.get("status", "failed")
    job_status = status_map.get(result_status, Status.FAILED)

    # A dry run can still end with the application genuinely submitted — most
    # often because the user finishes it by hand in the open browser: the agent
    # fills the form and stops, the user corrects a field and clicks Submit, and
    # the agent then observes the confirmation page. Whatever the cause, if a
    # submission really happened it must be recorded as REAL: the daily-cap query
    # filters mode='real', so leaving it as dry_run makes a live application
    # invisible to the cap and lets the cap under-count.
    mode = "dry_run" if dry_run else "real"
    if dry_run and result_status == "applied":
        log.warning(
            "apply.dry_run_submitted",
            job_id=job.id,
            detail="agent submitted during a dry run; recording as real for cap accounting",
        )
        mode = "real"

    app = Application(
        job_id=job.id,
        mode=mode,
        status=result_status,
        confirmation_text=result.get("confirmation_text") or None,
        pre_submit_screenshot=result.get("pre_submit_screenshot") or None,
        post_submit_screenshot=result.get("post_submit_screenshot") or None,
        # Fall back to `reason` for non-success outcomes. The browser agent
        # explains a needs_human/failed result in `reason` (which lands on
        # jobs.status_reason), and only sets `error_log` for hard errors — so
        # every needs_human row used to store a NULL error_log, and any report
        # built from the applications table showed blank reasons.
        error_log=(
            result.get("_error_log")
            or result.get("error_log")
            or (result.get("reason") if result_status in {"needs_human", "failed"} else None)
        ),
    )
    session.add(app)

    job.status = job_status
    job.status_reason = result.get("reason") or None

    log.info(
        "apply.persisted",
        job_id=job.id,
        result_status=result_status,
        job_status=job_status,
        mode=app.mode,
    )
    return app


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_apply(
    dry_run: bool = True,
    no_safety: bool = False,
    limit: int | None = None,
    url: str | None = None,
    enable_linkedin_easy_apply: bool = False,
) -> None:
    """Run the apply stage.

    MODE B: if ``url`` is given, generate apply_instructions.md and print path.
    MODE A: process COVERED jobs from the DB.
    """
    settings = get_settings()

    # ── MODE B ────────────────────────────────────────────────────────────────
    if url:
        ats_type = detect_ats(url)
        job_id = "adhoc"
        _, out_path = _build_instructions(
            job_url=url,
            job_id=job_id,
            ats_type=ats_type,
            dry_run=dry_run,
        )
        print(f"Instructions written to: {out_path}")
        return

    # ── MODE A ────────────────────────────────────────────────────────────────
    # Load all COVERED jobs
    with session_scope() as s:
        # DRY_RUN_COMPLETED jobs are included deliberately. The dry-run canary
        # exists to prove a form fills correctly before real submissions follow —
        # but it left its subject in a terminal state that apply never revisited,
        # so a fully prepared application (résumé + cover letter already paid for)
        # was silently never sent. Observed 2026-08-02 with 13 such jobs stranded,
        # including one batch of size 1 where the canary consumed the only job.
        # Re-queuing them means the canary validates, then the next pass submits.
        query = s.query(Job).filter(
            Job.status.in_((Status.COVERED, Status.DRY_RUN_COMPLETED))
        )
        if limit is not None:
            query = query.limit(limit)
        jobs = query.all()

    if not jobs:
        log.info("apply.no_covered_jobs")
        print("No jobs with status COVERED found.")
        return

    # Print first-N safety notice once
    if not no_safety:
        print(
            f"First {settings.honestapply_dry_run_first_n} are dry runs. "
            "Pass --no-safety to override after you've verified screenshots look correct."
        )

    # Track domain → last submit time for rate limiting (in-memory, this run only)
    last_submit: dict[str, float] = {}

    processed_this_run = 0

    for job in jobs:
        try:
            _process_job(
                job=job,
                dry_run=dry_run,
                no_safety=no_safety,
                enable_linkedin_easy_apply=enable_linkedin_easy_apply,
                settings=settings,
                last_submit=last_submit,
                processed_this_run=processed_this_run,
            )
        except Exception:
            tb = traceback.format_exc()
            log.error("apply.job_exception", job_id=job.id, traceback=tb)
            with session_scope() as s:
                j = s.get(Job, job.id)
                if j:
                    j.status = Status.FAILED
                    j.status_reason = "Unhandled exception in apply loop"
                    app = Application(
                        job_id=j.id,
                        mode="dry_run" if dry_run else "real",
                        status="failed",
                        error_log=tb,
                    )
                    s.add(app)
        processed_this_run += 1


def _process_job(
    job: Job,
    dry_run: bool,
    no_safety: bool,
    enable_linkedin_easy_apply: bool,
    settings,
    last_submit: dict,
    processed_this_run: int,
) -> None:
    """Process a single job through the full apply pipeline."""

    # ── SAFETY: first-N guard ─────────────────────────────────────────────────
    effective_dry_run = dry_run
    if not no_safety and processed_this_run < settings.honestapply_dry_run_first_n:
        effective_dry_run = True

    # ── TRIPLE DEDUP ──────────────────────────────────────────────────────────
    # Only a *real, completed* submission blocks a re-apply. Earlier dry-run or
    # needs_human rows never actually submitted, so they must NOT block a later
    # real attempt (otherwise a single dry-run permanently buries a good job).
    with session_scope() as s:
        # (a) skip if THIS job already has a real, applied submission
        if _has_real_submission(s, job.id):
            log.info("apply.dedup_job_id", job_id=job.id)
            return

        # (b) skip if another job with the same url_hash was really submitted
        if job.url_hash:
            same_hash_jobs = (
                s.query(Job)
                .filter(Job.url_hash == job.url_hash, Job.id != job.id)
                .all()
            )
            for other in same_hash_jobs:
                if _has_real_submission(s, other.id):
                    log.info("apply.dedup_url_hash", job_id=job.id, duplicate_of=other.id)
                    return

    # ── LINKEDIN guard ────────────────────────────────────────────────────────
    job_url = job.url or ""
    ats_type = job.ats_type or detect_ats(job_url)

    # ── DEAD-HOST guard ───────────────────────────────────────────────────────
    # Indeed postings expire quickly and the apply almost always lands on an
    # "expired" page → route to needs_human instead of burning a browser session.
    if "indeed." in (_domain(job_url) or ""):
        reason = "Indeed posting (expires fast; not a reliable apply path)"
        log.warning("apply.dead_host_skip", job_id=job.id, reason=reason)
        with session_scope() as s:
            j = s.get(Job, job.id)
            if j:
                j.status = Status.NEEDS_HUMAN
                j.status_reason = reason
                s.add(Application(job_id=j.id, mode="dry_run", status="needs_human", error_log=reason))
        return

    if ats_type == "linkedin":
        if not enable_linkedin_easy_apply:
            reason = "LinkedIn Easy Apply disabled (ban risk)"
            log.warning("apply.linkedin_skip", job_id=job.id, reason=reason)
            with session_scope() as s:
                j = s.get(Job, job.id)
                if j:
                    j.status = Status.NEEDS_HUMAN
                    j.status_reason = reason
                    s.add(Application(job_id=j.id, mode="dry_run", status="needs_human", error_log=reason))
            return
        confirm = os.environ.get("HONESTAPPLY_LINKEDIN_CONFIRM", "")
        if confirm != "I-UNDERSTAND-LINKEDIN-BAN-RISK":
            reason = (
                "LinkedIn Easy Apply flag set but HONESTAPPLY_LINKEDIN_CONFIRM env var "
                "not set to 'I-UNDERSTAND-LINKEDIN-BAN-RISK'. Skipping."
            )
            log.warning("apply.linkedin_missing_confirm", job_id=job.id)
            print(f"WARNING: {reason}")
            with session_scope() as s:
                j = s.get(Job, job.id)
                if j:
                    j.status = Status.NEEDS_HUMAN
                    j.status_reason = reason
                    s.add(Application(job_id=j.id, mode="dry_run", status="needs_human", error_log=reason))
            return

    # ── DAILY CAP (only for real submissions) ─────────────────────────────────
    if not effective_dry_run:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        with session_scope() as s:
            real_today = (
                s.query(Application)
                .filter(
                    Application.mode == "real",
                    Application.applied_at >= cutoff,
                )
                .count()
            )
        cap = min(settings.effective_daily_cap, HARD_DAILY_CEILING)
        if real_today >= cap:
            log.warning(
                "apply.daily_cap_reached",
                real_today=real_today,
                cap=cap,
            )
            print(
                f"Daily cap reached ({real_today}/{cap} real submissions today). "
                "Dry-runs still allowed."
            )
            # Force dry-run for cap safety but continue processing
            effective_dry_run = True

    # ── RATE LIMIT ────────────────────────────────────────────────────────────
    domain = _domain(job_url)
    if domain in last_submit and not _is_mock():
        import random
        jitter = random.uniform(
            -settings.honestapply_rate_limit_jitter_seconds,
            settings.honestapply_rate_limit_jitter_seconds,
        )
        wait = settings.honestapply_rate_limit_seconds + jitter
        elapsed = time.monotonic() - last_submit[domain]
        sleep_for = max(0.0, wait - elapsed)
        if sleep_for > 0:
            log.info("apply.rate_limit_sleep", domain=domain, seconds=round(sleep_for, 1))
            time.sleep(sleep_for)

    # ── GENERATE INSTRUCTIONS ─────────────────────────────────────────────────
    instructions_text, out_path = _build_instructions(
        job_url=job_url,
        job_id=job.id,
        ats_type=ats_type,
        dry_run=effective_dry_run,
    )
    log.info("apply.instructions_written", job_id=job.id, path=str(out_path))

    # ── EXECUTE ───────────────────────────────────────────────────────────────
    job_dir = PATHS.job_output_dir(job.id)

    if _is_mock():
        result = _mock_result(effective_dry_run, job_dir)
    else:
        result = _run_claude(instructions_text)

    # ── PERSIST ───────────────────────────────────────────────────────────────
    with session_scope() as s:
        j = s.get(Job, job.id)
        if j:
            _persist(s, j, result, effective_dry_run)

    # Track last submit time for rate limiting
    last_submit[domain] = time.monotonic()
