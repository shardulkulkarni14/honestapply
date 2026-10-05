"""The `inbox` stage: turn recruiter/ATS emails into status updates.

Flow, per run:
  1. list recent Gmail message ids (last N days), skip any already ingested;
  2. fetch + parse each new message;
  3. shortlist the jobs you have *applied to* whose company the email mentions;
  4. ask the LLM to pick the matching job and classify the email
     (application_received / interview_invite / rejection / offer / info_request
     / other) with a 0-100 confidence;
  5. on a confident, clear match, move the job's status inside
     ``transition(source="email")`` so the change is attributed in job_events;
  6. record the email either way, so it is classified exactly once.

Honest by construction: email content is untrusted (attacker-controllable), so
it is fenced before the model sees it, a status only changes on a confident
match, and transitions never regress a job to an earlier stage.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import select

from honestapply.config import get_settings
from honestapply.db.events import transition
from honestapply.db.models import EmailMessage, Job, Status
from honestapply.db.session import session_scope
from honestapply.email.gmail import ParsedEmail, get_message, get_service, list_recent_ids
from honestapply.llm.base import LLMProvider, get_provider
from honestapply.llm.untrusted import fence
from honestapply.logging_setup import get_logger

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Jobs an inbound email could plausibly be about: ones you have actually applied
# to, their post-apply outcomes, and ones parked for a human. Never a
# still-in-pipeline (discovered/scored/...) job — no recruiter has written yet.
_TRACKED_STATES = [
    Status.APPLIED,
    Status.SCREENING,
    Status.INTERVIEWING,
    Status.OFFER,
    Status.REJECTED,
    Status.GHOSTED,
    Status.NEEDS_HUMAN,
]

# event_type -> the status it implies.
_EVENT_STATUS = {
    "application_received": Status.APPLIED,
    "interview_invite": Status.INTERVIEWING,
    "rejection": Status.REJECTED,
    "offer": Status.OFFER,
    # info_request / other -> no status change (recorded only).
}

# Forward-only ranking so a late "application received" can't drag an
# interviewing job back to applied. Rejection and offer are terminal outcomes
# allowed from any state.
_RANK = {Status.APPLIED: 1, Status.SCREENING: 2, Status.INTERVIEWING: 3, Status.OFFER: 4}

_LEGAL_SUFFIXES = re.compile(
    r"\b(gmbh|mbh|inc|inc\.|llc|ltd|ltd\.|limited|ag|se|plc|co|corp|corporation|"
    r"pvt|private|technologies|technology|labs|group|holdings)\b",
    re.IGNORECASE,
)


def _normalize_company(name: str) -> str:
    name = _LEGAL_SUFFIXES.sub(" ", name or "")
    return re.sub(r"[^a-z0-9 ]+", " ", name.lower()).strip()


def _email_mentions(email: ParsedEmail, company_norm: str) -> bool:
    """True if a distinctive token of the company appears in the email."""
    tokens = [t for t in company_norm.split() if len(t) >= 3]
    if not tokens:
        return False
    hay = " ".join(
        [email.sender_domain, email.subject or "", email.snippet or "", (email.body or "")[:2000]]
    ).lower()
    # Match on the longest token (most distinctive) to curb false positives like
    # "data" or "tech" hitting unrelated mail.
    return max(tokens, key=len) in hay


def _candidate_jobs(session, email: ParsedEmail, limit: int = 15) -> list[Job]:
    jobs = list(
        session.execute(
            select(Job).where(Job.status.in_(_TRACKED_STATES)).order_by(Job.updated_at.desc())
        ).scalars()
    )
    matches = [j for j in jobs if _email_mentions(email, _normalize_company(j.company))]
    return matches[:limit]


def _decide_status(current: str, event_type: str) -> str | None:
    """The status this email should move a job to, or None for no change.

    Enforces: terminal outcomes (rejection/offer) from any state; forward-only
    progression otherwise; and never a no-op (returns None if already there)."""
    target = _EVENT_STATUS.get(event_type)
    if target is None or target == current:
        return None
    if event_type in ("rejection", "offer"):
        return target
    # Progress events: only move forward through applied -> screening ->
    # interviewing -> offer. A needs_human job counts as pre-progress (rank 0).
    cur_rank = _RANK.get(current, 0)
    if _RANK.get(target, 0) > cur_rank:
        return target
    return None


def _load_prompt(name: str, **kwargs: Any) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").format(**kwargs)


def _classify(provider: LLMProvider, email: ParsedEmail, candidates: list[Job]) -> dict[str, Any]:
    cand_lines = "\n".join(f"- id={j.id} | {j.company} | {j.title} | status={j.status}" for j in candidates)
    prompt = _load_prompt(
        "classify_email",
        candidates=cand_lines or "(none)",
        sender=email.sender,
        subject=email.subject or "",
        body=fence((email.body or email.snippet or "")[:4000], label="EMAIL", description="email body"),
    )
    try:
        return provider.complete_json(prompt)
    except Exception as exc:  # noqa: BLE001 - a bad classification must not abort the run
        logger.warning("inbox.classify_failed", msg_id=email.msg_id, error=str(exc))
        return {}


def run_inbox(
    *,
    lookback_days: int | None = None,
    max_results: int = 100,
    min_confidence: int | None = None,
    service: Any = None,
    provider: LLMProvider | None = None,
) -> dict[str, int]:
    """Sync Gmail into the tracker. Returns a counts summary.

    *service* / *provider* are injectable for tests; in normal use they default
    to a connected Gmail service and the configured LLM provider.
    """
    settings = get_settings()
    lookback_days = lookback_days if lookback_days is not None else settings.honestapply_inbox_lookback_days
    min_confidence = (
        min_confidence if min_confidence is not None else settings.honestapply_inbox_min_confidence
    )
    service = service or get_service()
    provider = provider or get_provider(settings)

    msg_ids = list_recent_ids(service, after_days=lookback_days, max_results=max_results)
    with session_scope() as s:
        seen = set(s.execute(select(EmailMessage.gmail_msg_id)).scalars())
    new_ids = [m for m in msg_ids if m not in seen]
    logger.info("inbox: %d recent messages, %d new", len(msg_ids), len(new_ids))

    counts = {"scanned": len(new_ids), "matched": 0, "updated": 0, "recorded": 0}

    for mid in new_ids:
        email = get_message(service, mid)
        matched_job_id: int | None = None
        classification: str | None = None
        confidence = 0

        with session_scope() as s:
            candidates = _candidate_jobs(s, email)
            cand_ids = {j.id for j in candidates}

            if candidates:
                result = _classify(provider, email, candidates)
                event_type = str(result.get("event_type", "other")).strip().lower()
                confidence = _coerce_int(result.get("confidence"))
                raw_job = result.get("job_id")
                job_id = raw_job if isinstance(raw_job, int) and raw_job in cand_ids else None
                classification = event_type if event_type != "other" else None

                if job_id is not None and confidence >= min_confidence:
                    matched_job_id = job_id
                    counts["matched"] += 1
                    job = s.get(Job, job_id)
                    new_status = _decide_status(job.status, event_type) if job else None
                    if new_status:
                        note = f"{event_type} via email: {email.subject or ''}".strip()
                        with transition("email", note=note[:500]):
                            job.status = new_status
                            # Flush now, inside the transition block, so the
                            # before_flush listener stamps the job_event with
                            # source='email' rather than the default 'pipeline'
                            # (which is what the later session commit would use).
                            s.flush()
                        counts["updated"] += 1
                        logger.info(
                            "inbox: job %d %s -> %s (%s, conf=%d)",
                            job_id, job.status, new_status, event_type, confidence,
                        )

            s.add(
                EmailMessage(
                    job_id=matched_job_id,
                    gmail_msg_id=email.msg_id,
                    thread_id=email.thread_id or None,
                    sender=email.sender or None,
                    subject=(email.subject or None),
                    snippet=(email.snippet or None),
                    received_at=email.received_at,
                    classification=classification,
                    confidence=confidence or None,
                )
            )
            counts["recorded"] += 1

    return counts


def _coerce_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
