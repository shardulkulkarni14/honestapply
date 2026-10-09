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

import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from sqlalchemy import func

from honestapply.ats.detect import detect_ats
from honestapply.config import (
    HARD_DAILY_CEILING,
    PATHS,
    get_settings,
    load_answers,
    load_ats_selectors,
    load_profile,
)
from honestapply.db.models import Application, Job, Status
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
    account_signup: bool = False,
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

    # Deterministic fast-path: precompute the truthful, placeholder-free fields we
    # can fill without per-field LLM reasoning, and hand them to the agent as an
    # explicit plan. Conservative on country (defer when unknown) so work-auth and
    # India-phone are never guessed; everything unplanned falls back to Steps 4–8.
    from honestapply.ats.fieldmap import build_field_plan, render_fill_plan

    field_plan = build_field_plan(ats_type, profile, answers, ats_selectors, target_country="")
    fill_plan_str = render_fill_plan(field_plan)
    (job_dir / "fill_plan.json").write_text(
        json.dumps(field_plan.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    substitutions = {
        "job_url": job_url,
        "ats_type": ats_type,
        "dry_run": str(dry_run),
        "account_signup": str(account_signup),
        "resume_pdf_path": str(resume_pdf.resolve()),
        "cover_letter_pdf_path": str(cover_letter_pdf.resolve()),
        "recommendation_pdf_path": rec_pdf,
        "profile_json": profile_json_str,
        "answers_yaml": answers_yaml_str,
        "ats_selectors": ats_selectors_str,
        "fill_plan": fill_plan_str,
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


# The agent prints this one-liner when it hits a CAPTCHA / needs a human, BEFORE
# waiting in-session: honestapply sees it live and pings the human (with the
# live-view URL) so they can solve it remotely and let the same run continue.
_NOTIFY_RE = re.compile(r"<<<NOTIFY>>>\s*(\{.*?\})\s*<<<END>>>", re.DOTALL)


def _parse_claude_result(stdout: str) -> dict:
    """Parse the LAST <<<RESULT>>> block from the agent's stdout."""
    matches = _RESULT_RE.findall(stdout)
    if not matches:
        tail = stdout[-2000:] if len(stdout) > 2000 else stdout
        return {"status": "failed", "reason": "No <<<RESULT>>> block found in claude output", "confirmation_text": "", "pre_submit_screenshot": "", "post_submit_screenshot": "", "_error_log": tail}
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        return {"status": "failed", "reason": f"JSON parse error in result block: {exc}", "confirmation_text": "", "pre_submit_screenshot": "", "post_submit_screenshot": "", "_error_log": matches[-1]}


def _run_claude(
    instructions_text: str,
    *,
    mcp_config: str | None = None,
    extra_env: dict[str, str] | None = None,
    notify_cb=None,
    timeout: int = 600,
) -> dict:
    """Invoke the claude CLI and parse the <<<RESULT>>> block from stdout.

    *mcp_config* is passed as ``--mcp-config <path> --strict-mcp-config`` so the
    agent uses exactly that server set (account-signup proxy and/or a hosted-browser
    CDP endpoint). *extra_env* is merged into the subprocess env. When *notify_cb*
    is given, stdout is streamed and ``notify_cb(payload)`` is called for each
    ``<<<NOTIFY>>>{…}<<<END>>>`` line the agent emits (so we can ping the human
    mid-run); otherwise the call is a simple capture."""
    apply_model = os.environ.get("HONESTAPPLY_APPLY_MODEL", "claude-opus-5")
    cmd = ["claude", "--dangerously-skip-permissions", "--model", apply_model]
    if mcp_config:
        cmd += ["--mcp-config", mcp_config, "--strict-mcp-config"]
    cmd += ["-p", instructions_text]
    env = {**os.environ, **extra_env} if extra_env else None

    if notify_cb is None:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return {"status": "failed", "reason": f"claude subprocess timed out ({timeout}s)", "confirmation_text": "", "pre_submit_screenshot": "", "post_submit_screenshot": ""}
        except Exception as exc:
            return {"status": "failed", "reason": f"claude subprocess error: {exc}", "confirmation_text": "", "pre_submit_screenshot": "", "post_submit_screenshot": ""}
        return _parse_claude_result(proc.stdout or "")

    # Streaming path: watch for <<<NOTIFY>>> lines as they arrive.
    import threading

    lines: list[str] = []
    seen: set[str] = set()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "reason": f"claude subprocess error: {exc}", "confirmation_text": "", "pre_submit_screenshot": "", "post_submit_screenshot": ""}

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            m = _NOTIFY_RE.search(line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                try:
                    notify_cb(json.loads(m.group(1)))
                except Exception as exc:  # noqa: BLE001 — a bad ping must not abort
                    log.warning("apply.notify_cb_failed", error=str(exc))

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        t.join(timeout=5)
        return {"status": "needs_human", "reason": f"Timed out after {timeout}s waiting (e.g. for a human to solve a CAPTCHA).", "confirmation_text": "", "pre_submit_screenshot": "", "post_submit_screenshot": "", "_error_log": "".join(lines)[-2000:]}
    t.join(timeout=5)
    return _parse_claude_result("".join(lines))


def _build_apply_mcp_config(
    job_id: int | str,
    *,
    via_proxy: bool = False,
) -> str:
    """Write a per-run MCP config derived from .mcp.json, optionally wrapping the
    `playwright` server in the secret-injection proxy (*via_proxy*, for account
    signup). Every other server (e.g. `gmail`) is preserved. Returns its path."""
    base = PATHS.root / ".mcp.json"
    cfg = json.loads(base.read_text(encoding="utf-8")) if base.exists() else {}
    servers = cfg.setdefault("mcpServers", {})
    pw = servers.get("playwright")
    if pw:
        inner = [pw.get("command", "npx"), *pw.get("args", [])]
        if via_proxy:
            # sys.executable (not bare "python") so the proxy resolves to the env
            # honestapply runs in, regardless of the agent subprocess PATH.
            servers["playwright"] = {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "honestapply.secret_proxy", *inner],
                "env": pw.get("env", {}),
            }
        else:
            servers["playwright"] = {"type": "stdio", "command": inner[0], "args": inner[1:], "env": pw.get("env", {})}
    out = PATHS.job_output_dir(job_id) / "apply_mcp.json"
    out.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return str(out)


def _write_signup_mcp_config(job_id: int | str) -> str:
    """Account-signup MCP config: playwright wrapped by the secret proxy."""
    return _build_apply_mcp_config(job_id, via_proxy=True)


def _signup_env(job_url: str) -> dict[str, str]:
    """Env for a signup run: the site key (for the vault/proxy) and the applicant
    email the account is created under."""
    from honestapply.vault import normalize_site

    profile = load_profile()
    return {
        "HONESTAPPLY_ACCOUNT_SITE": normalize_site(job_url),
        "HONESTAPPLY_ACCOUNT_EMAIL": profile.email or "",
    }


def _execute_agent(job: Job, job_url: str, instructions: str, signup_mode: bool, settings) -> dict:
    """Run the apply agent for one job, with human-in-the-loop notifications (ping
    you on a CAPTCHA / needs-human stop so you can solve it in the open browser and
    the SAME run resumes) and the account-signup secret proxy. Fully local — no
    third-party browser."""
    from honestapply import notify as notify_mod

    notify_enabled = notify_mod.is_configured(settings)
    extra_env = _signup_env(job_url) if signup_mode else None
    mcp_config = _write_signup_mcp_config(job.id) if signup_mode else None
    run_timeout = 600
    notify_cb = None

    if notify_enabled:
        company = job.company or ""
        # Give you time to solve a CAPTCHA in the live browser before giving up.
        run_timeout = max(600, getattr(settings, "honestapply_captcha_wait_seconds", 240) + 180)

        def notify_cb(payload: dict) -> None:
            reason = payload.get("reason") or payload.get("type") or "a step needs you"
            notify_mod.notify(f"honestapply needs you · {company}".strip(" ·"), str(reason))

    return _run_claude(
        instructions,
        mcp_config=mcp_config,
        extra_env=extra_env,
        notify_cb=notify_cb,
        timeout=run_timeout,
    )


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
        jobs = query.all()

    # Spread employers across the run. The query returns rows in id order, which
    # groups a company's roles together and can send several back-to-back
    # applications to one employer in a single run — spraying rather than
    # interest. Interleaving alone is not enough (a company with many open roles
    # still gets several per run), so `_over_company_cap` in _process_job enforces
    # a rolling-24h per-employer limit as well.
    jobs = _interleave_by_company(jobs)
    if limit is not None:
        jobs = jobs[:limit]

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


def _interleave_by_company(jobs: list[Job]) -> list[Job]:
    """Reorder so consecutive jobs come from different employers where possible.

    Round-robins across per-company queues: one job from each company in turn,
    then the next from each, and so on. Companies with more open roles simply
    reappear in later rounds instead of monopolising a contiguous block.

    With a single employer in the list the order is unchanged — nothing to
    interleave. Relative order within a company is preserved.
    """
    by_company: dict[str, list[Job]] = {}
    for job in jobs:
        key = (job.company or "").strip().lower()
        by_company.setdefault(key, []).append(job)

    ordered: list[Job] = []
    queues = list(by_company.values())
    while queues:
        # Drop companies whose queue is exhausted, then take one from each.
        queues = [q for q in queues if q]
        for q in queues:
            ordered.append(q.pop(0))
    return ordered


def _over_company_cap(session, company: str, cap: int) -> bool:
    """True if this employer already received ``cap`` real submissions in 24h.

    Counts DISTINCT job_id rather than Application rows: the apply stage can
    write more than one row for the same job, so a plain row count would
    overstate how many roles the employer actually saw.
    """
    if cap <= 0 or not company:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = (
        session.query(func.count(func.distinct(Application.job_id)))
        .join(Job, Job.id == Application.job_id)
        .filter(
            func.lower(func.trim(Job.company)) == company.strip().lower(),
            Application.mode == "real",
            Application.status == "applied",
            Application.applied_at >= cutoff,
        )
        .scalar()
    ) or 0
    return recent >= cap


def _company_total_submissions(session, company: str) -> int:
    """All-time count of DISTINCT roles this employer has been really applied to.

    The 24h cap above only stops same-day spraying; it resets overnight, so a
    company can still accumulate many applications across days (observed: Holidu
    reached 4+ over Sep 5-8). This cumulative count backs a hard lifetime cap so
    the pipeline never keeps re-applying to one employer role after role.
    """
    if not company:
        return 0
    return (
        session.query(func.count(func.distinct(Application.job_id)))
        .join(Job, Job.id == Application.job_id)
        .filter(
            func.lower(func.trim(Job.company)) == company.strip().lower(),
            Application.mode == "real",
            Application.status == "applied",
        )
        .scalar()
    ) or 0


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

    # Set when an account-walled job is handled by auto-signup instead of being
    # routed to a human (opt-in; see the ACCOUNT-WALLED guard below).
    signup_mode = False

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

        # (c) respect the per-employer rolling-24h cap. Several roles at one
        # company is fine over time; several in one sitting is spraying. The job
        # stays COVERED so a later run can send it once the window clears — this
        # is a deferral, not a rejection.
        company_cap = getattr(settings, "honestapply_per_company_daily_cap", 0)
        if _over_company_cap(s, job.company or "", company_cap):
            log.info(
                "apply.company_cap_reached",
                job_id=job.id,
                company=job.company,
                cap=company_cap,
            )
            print(f"  SKIP (company cap {company_cap}/24h reached): {job.company}")
            return

        # (d) cumulative lifetime cap. The 24h cap resets overnight, so without
        # this a company keeps collecting applications day after day (role after
        # role), which reads as spam. Once an employer has this many real
        # submissions total, RETIRE the job (terminal status) so it drops out of
        # the COVERED/DRY_RUN_COMPLETED queue instead of being re-tried forever.
        total_cap = getattr(settings, "honestapply_per_company_total_cap", 2)
        if total_cap > 0 and _company_total_submissions(s, job.company or "") >= total_cap:
            log.info(
                "apply.company_total_cap_reached",
                job_id=job.id,
                company=job.company,
                cap=total_cap,
            )
            print(f"  RETIRE (company lifetime cap {total_cap} reached): {job.company}")
            jrow = s.get(Job, job.id)
            if jrow is not None:
                jrow.status = Status.SKIPPED_COMPANY_CAP
                jrow.status_reason = (
                    f"Retired: employer already has {total_cap}+ submitted "
                    f"applications (lifetime cap), not re-applying to avoid spam."
                )
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

    # ── ACCOUNT-WALLED guard ──────────────────────────────────────────────────
    # Some ATSes (Haufe Umantis, SAP SuccessFactors — common for German
    # employers) require a portal account before any form can be filled. There is
    # no guest-apply path, so route to a human up front rather than spending a
    # browser session discovering the wall.
    from honestapply.ats.detect import is_account_walled

    if is_account_walled(ats_type):
        if getattr(settings, "honestapply_enable_account_signup", False):
            # Opt-in: don't bail to a human. Create an account (email + a
            # vault-managed password the agent never sees) and continue through the
            # normal dry-run/cap/rate-limit guards below, in signup mode.
            signup_mode = True
            log.info("apply.account_signup_enabled", job_id=job.id, ats_type=ats_type)
        else:
            reason = f"{ats_type} requires a portal account (no guest apply)"
            log.warning("apply.account_walled_skip", job_id=job.id, reason=reason)
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
        account_signup=signup_mode,
    )
    log.info("apply.instructions_written", job_id=job.id, path=str(out_path), signup=signup_mode)

    # ── EXECUTE ───────────────────────────────────────────────────────────────
    job_dir = PATHS.job_output_dir(job.id)

    if _is_mock():
        result = _mock_result(effective_dry_run, job_dir)
    else:
        result = _execute_agent(job, job_url, instructions_text, signup_mode, settings)

    # ── PERSIST ───────────────────────────────────────────────────────────────
    with session_scope() as s:
        j = s.get(Job, job.id)
        if j:
            _persist(s, j, result, effective_dry_run)

    # Track last submit time for rate limiting
    last_submit[domain] = time.monotonic()
