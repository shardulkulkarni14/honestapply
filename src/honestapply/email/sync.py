"""The `inbox` stage: turn recruiter / ATS emails into status updates.

honestapply is an agentic tool, so this stage works like the apply stage: it
spawns a ``claude`` agent that reads Gmail over **MCP tool calls** (the Gmail MCP
server in ``.mcp.json``) and returns a structured verdict per message. This module
never touches the Gmail API directly — it builds the triage instructions, runs the
agent (``email.agent``), then applies the honest guards to whatever the agent
reports and writes the results to the tracker.

Flow, per run:
  1. shortlist the jobs an email could be about — ones you've *applied to* (and
     their post-apply outcomes), never a still-in-pipeline job;
  2. hand that shortlist + the already-seen message ids to the triage agent, which
     searches and reads the last N days of mail via Gmail MCP and classifies each
     relevant message (application_received / interview_invite / rejection /
     offer / info_request / other) with a 0-100 confidence;
  3. for each message the agent returns: skip ones already ingested (dedup),
     verify the match actually names the company (sanity check against an agent
     over-match), and on a confident, clear match move the job inside
     ``transition(source="email")`` so the change is attributed in job_events;
  4. record the message either way, so it is classified exactly once.

Honest by construction: email content is untrusted (the agent is told never to
follow instructions inside a message and never to send/modify anything), a status
only changes on a confident match that independently mentions the company, and
transitions never regress a job to an earlier stage.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import select

from honestapply.config import get_settings
from honestapply.db.events import transition
from honestapply.db.models import EmailMessage, Job, Status
from honestapply.db.session import session_scope
from honestapply.email.agent import AgentRunner, default_runner, render_instructions
from honestapply.logging_setup import get_logger

logger = get_logger(__name__)

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

# Final outcomes: once here, a *progress* email (e.g. a stale "application
# received" ack that arrives after the rejection) must not resurrect the job.
# Only another terminal event (rejection<->offer) can change it. GHOSTED is
# deliberately NOT terminal — a reply after silence legitimately un-ghosts a job.
_TERMINAL = {Status.REJECTED, Status.OFFER}

_LEGAL_SUFFIXES = re.compile(
    r"\b(gmbh|mbh|inc|inc\.|llc|ltd|ltd\.|limited|ag|se|plc|co|corp|corporation|"
    r"pvt|private|technologies|technology|labs|group|holdings)\b",
    re.IGNORECASE,
)


def _normalize_company(name: str) -> str:
    name = _LEGAL_SUFFIXES.sub(" ", name or "")
    return re.sub(r"[^a-z0-9 ]+", " ", name.lower()).strip()


def _mentions_company(hay: str, company_norm: str) -> bool:
    """True if a distinctive token of the company appears in *hay*.

    A sanity check applied to the agent's chosen match: even though the agent
    picks the job_id, we only accept a *status-changing* match when the message's
    own sender/subject/snippet independently names the company — a cheap guard
    against the agent attributing an email to the wrong (or a hallucinated) job.
    """
    tokens = [t for t in company_norm.split() if len(t) >= 3]
    if not tokens:
        return False
    # Match on the longest token (most distinctive) to curb false positives like
    # "data" or "tech" hitting unrelated mail.
    return max(tokens, key=len) in (hay or "").lower()


def _decide_status(current: str, event_type: str) -> str | None:
    """The status this email should move a job to, or None for no change.

    Enforces: terminal outcomes (rejection/offer) from any state; forward-only
    progression otherwise; and never a no-op (returns None if already there)."""
    target = _EVENT_STATUS.get(event_type)
    if target is None or target == current:
        return None
    if event_type in ("rejection", "offer"):
        return target
    # Progress events never resurrect a final outcome (rejected/offer).
    if current in _TERMINAL:
        return None
    # Otherwise only move forward through applied -> screening -> interviewing ->
    # offer. A needs_human / ghosted job counts as pre-progress (rank 0).
    cur_rank = _RANK.get(current, 0)
    if _RANK.get(target, 0) > cur_rank:
        return target
    return None


def _coerce_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_received_at(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _candidate_lines(jobs: list[Job]) -> str:
    """The `id | company | role | status` block the agent is scoped to."""
    return "\n".join(
        f"{j.id} | {j.company} | {j.title} | {j.status}" for j in jobs
    ) or "(none)"


def run_inbox(
    *,
    lookback_days: int | None = None,
    max_results: int = 100,
    min_confidence: int | None = None,
    max_candidates: int | None = None,
    agent_runner: AgentRunner | None = None,
) -> dict[str, int]:
    """Sync Gmail into the tracker via the triage agent. Returns a counts summary.

    *agent_runner* is injectable for tests/sim; in normal use it defaults to a
    real ``claude`` subprocess with Gmail MCP (or an offline no-op when
    ``HONESTAPPLY_INBOX_MOCK`` is set).
    """
    settings = get_settings()
    lookback_days = (
        lookback_days if lookback_days is not None else settings.honestapply_inbox_lookback_days
    )
    min_confidence = (
        min_confidence if min_confidence is not None else settings.honestapply_inbox_min_confidence
    )
    max_candidates = (
        max_candidates if max_candidates is not None else settings.honestapply_inbox_max_candidates
    )
    agent_runner = agent_runner or default_runner()

    counts = {"scanned": 0, "matched": 0, "updated": 0, "recorded": 0}

    # 1. Shortlist candidate jobs + the message ids already ingested. Cap to the
    # most-recently-updated tracked jobs: inbound mail is almost always about a
    # recent application, and an unbounded list bloats the prompt.
    with session_scope() as s:
        candidates = list(
            s.execute(
                select(Job)
                .where(Job.status.in_(_TRACKED_STATES))
                .order_by(Job.updated_at.desc())
                .limit(max_candidates)
            ).scalars()
        )
        company_by_id = {j.id: j.company for j in candidates}
        cand_ids = set(company_by_id)
        candidate_block = _candidate_lines(candidates)
        seen = set(s.execute(select(EmailMessage.gmail_msg_id)).scalars())

    if not candidates:
        logger.info("inbox: no applied/tracked jobs — nothing an email could be about")
        return counts

    # 2. Run the triage agent (Gmail MCP tool calls happen inside it).
    seen_block = "\n".join(sorted(seen)) or "(none)"
    instructions = render_instructions(
        candidates=candidate_block,
        seen_ids=seen_block,
        lookback_days=lookback_days,
        max_results=max_results,
    )
    result = agent_runner(instructions)
    messages = result.get("messages") or []
    logger.info("inbox: agent returned %d message verdict(s)", len(messages))

    # 3. Apply the honest guards to each returned message and persist.
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        mid = str(msg.get("msg_id") or "").strip()
        if not mid or mid in seen:
            continue  # dedup: never record / re-classify a message twice
        seen.add(mid)

        counts["scanned"] += 1
        event_type = str(msg.get("event_type", "other")).strip().lower()
        confidence = _coerce_int(msg.get("confidence"))
        raw_job = msg.get("job_id")
        job_id = raw_job if isinstance(raw_job, int) and raw_job in cand_ids else None
        classification = event_type if event_type != "other" else None

        # Independent company-mention sanity check before we trust a match.
        hay = " ".join(
            str(msg.get(k) or "") for k in ("sender", "subject", "snippet")
        )
        if job_id is not None and not _mentions_company(
            hay, _normalize_company(company_by_id.get(job_id, ""))
        ):
            logger.info("inbox: dropping msg %s — agent match didn't name the company", mid)
            job_id = None

        matched_job_id: int | None = None
        with session_scope() as s:
            if job_id is not None and confidence >= min_confidence:
                matched_job_id = job_id
                counts["matched"] += 1
                job = s.get(Job, job_id)
                new_status = _decide_status(job.status, event_type) if job else None
                if new_status:
                    note = f"{event_type} via email: {msg.get('subject') or ''}".strip()
                    with transition("email", note=note[:500]):
                        job.status = new_status
                        # Flush now, inside the transition block, so the
                        # before_flush listener stamps the job_event with
                        # source='email' rather than the default 'pipeline'
                        # (which is what the later session commit would use).
                        s.flush()
                    counts["updated"] += 1
                    logger.info(
                        "inbox: job %d -> %s (%s, conf=%d)",
                        job_id, new_status, event_type, confidence,
                    )

            s.add(
                EmailMessage(
                    job_id=matched_job_id,
                    gmail_msg_id=mid,
                    thread_id=str(msg.get("thread_id") or "") or None,
                    sender=str(msg.get("sender") or "") or None,
                    subject=str(msg.get("subject") or "") or None,
                    snippet=str(msg.get("snippet") or "") or None,
                    received_at=_parse_received_at(msg.get("received_at")),
                    classification=classification,
                    confidence=confidence or None,
                )
            )
            counts["recorded"] += 1

    return counts
