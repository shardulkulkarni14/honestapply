"""Manual-assist apply packets for captcha-gated / login-walled forms.

Some ATS paths cannot be auto-submitted: Lever throws hCaptcha, Workday and some
SmartRecruiters tenants require a login, and CAPTCHAs are never solved by the
bot. Rather than burn a browser session that always ends in needs_human, this
stage assembles everything a human (or an interactive browser session) needs to
finish the application in one place:

  data/outputs/<job_id>/apply_packet.md   — URL, mapped field answers, doc paths
  data/outputs/<job_id>/transcripts_and_references.pdf  — merged supporting docs

The job is moved to NEEDS_HUMAN with a clear "manual-assist packet ready" reason.
Every value comes from the real profile/answers config — nothing is fabricated;
fields without a truthful answer are left blank for the human to decide.

Entry point:
    run_manual_assist(ids=None, limit=None) -> int   # packets built
"""

from __future__ import annotations

from pathlib import Path

import re

from honestapply.config import PATHS, load_answers, load_profile
from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.documents import build_bundle
from honestapply.logging_setup import get_logger

log = get_logger(__name__)

# ATS paths that realistically need a human to finish (captcha / login wall).
MANUAL_ATS = ("lever", "workday", "smartrecruiters", "generic", "linkedin")


_INDIA_LOCATION = re.compile(
    r"\b(india|bengaluru|bangalore|hyderabad|pune|mumbai|chennai|delhi|gurgaon|"
    r"gurugram|noida|kolkata|ahmedabad|telangana|karnataka|maharashtra)\b",
    re.IGNORECASE,
)


def _phone_for(profile, job_location: str) -> str:
    """Pick the phone number a recruiter for THIS job would actually dial.

    The browser apply path already switches to `phone_india` for India-based
    roles, but the packet builder read `profile.phone` directly and so printed
    the default number on India packets.
    """
    if job_location and _INDIA_LOCATION.search(job_location):
        india = getattr(profile, "phone_india", None) or (
            profile.model_extra or {}
        ).get("phone_india")
        if india:
            return str(india)
    return profile.phone


def _field_answers(profile, answers: dict, job_location: str = "") -> list[tuple[str, str]]:
    """Build the common application field answers from real profile/config data."""
    name = " ".join(str(profile.legal_name.get(k, "")) for k in ("first", "last")).strip()
    addr = profile.address or {}
    loc = ", ".join(str(addr.get(k, "")) for k in ("city", "state", "country") if addr.get(k))
    wa = profile.work_authorization or {}
    sal = profile.salary_expectation or {}
    sdq = profile.screening_question_defaults or {}
    rows = [
        ("Full name", name),
        ("Email", profile.email),
        ("Phone", _phone_for(profile, job_location)),
        ("Current location", loc),
        ("LinkedIn", profile.linkedin_url),
        ("GitHub", profile.github_url),
        ("Portfolio", profile.portfolio_url),
        ("Authorized to work in Germany", sdq.get("authorized_to_work", "Yes" if wa.get("eu_authorized") else "")),
        ("Needs visa sponsorship", sdq.get("requires_visa_sponsorship", "No" if not wa.get("sponsorship_needed") else "Yes")),
        ("Salary expectation", sdq.get("salary_expectation_text", "")
            or (f"From €{sal.get('min'):,} gross" if sal.get("min") else "")),
        ("Earliest start", sdq.get("earliest_start_date", profile.available_start_date)),
        ("Notice period", sdq.get("notice_period", "")),
    ]
    return [(k, v) for k, v in rows if v]


def build_apply_packet(job: Job) -> Path:
    """Write data/outputs/<id>/apply_packet.md and merge the supporting-doc bundle."""
    out_dir = PATHS.outputs_dir / str(job.id)
    out_dir.mkdir(parents=True, exist_ok=True)

    bundle = build_bundle(out_dir / "transcripts_and_references.pdf")

    profile = load_profile()
    answers = load_answers()
    fields = _field_answers(profile, answers, job.location or "")

    resume = job.tailored_resume_path or "(not generated)"
    cover = job.cover_letter_path or "(not generated)"

    lines = [
        f"# Manual apply packet — {job.company} · {job.title}",
        "",
        f"- **Apply URL:** {job.url}",
        f"- **ATS:** {job.ats_type or 'unknown'}",
        f"- **Résumé:** {resume}",
        f"- **Cover letter:** {cover}",
        f"- **Transcripts & references (merged):** {bundle if bundle else '(no supporting docs configured)'}",
        "",
        "## Field answers (from your profile — verify before submitting)",
        "",
        "| Field | Value |",
        "| --- | --- |",
    ]
    lines += [f"| {k} | {v} |" for k, v in fields]
    lines += [
        "",
        "## To finish",
        "1. Open the apply URL.",
        "2. Upload the résumé (and the merged transcripts/references PDF where required).",
        "3. Fill the fields above; leave anything you can't answer truthfully blank.",
        "4. Solve any CAPTCHA and click Submit yourself.",
        "",
        "_Nothing here is auto-submitted. Values are from your real profile; no fabrication._",
    ]
    packet = out_dir / "apply_packet.md"
    packet.write_text("\n".join(lines), encoding="utf-8")
    log.info("manual_assist.packet", job_id=job.id, packet=str(packet))
    return packet


def run_manual_assist(ids: list[int] | None = None, limit: int | None = None) -> int:
    """Build manual-assist packets for COVERED jobs and route them to NEEDS_HUMAN."""
    built = 0
    with session_scope() as session:
        query = session.query(Job).filter(Job.status == Status.COVERED)
        if ids:
            query = query.filter(Job.id.in_(ids))
        if limit:
            query = query.limit(limit)
        for job in query.all():
            packet = build_apply_packet(job)
            job.status = Status.NEEDS_HUMAN
            job.status_reason = f"manual-assist: packet ready at {packet}"
            built += 1
    log.info("manual_assist.done", built=built)
    return built
