"""Offline tests for the MCP-based Gmail inbox sync.

No network, no subprocess, no Google libs: the triage *agent* is injected as a
fake runner returning canned verdicts (the same shape the real `claude` agent
prints in its <<<RESULT>>> block), so these exercise honestapply's own guards —
matching + company-mention sanity check, the status transition (attributed to
source='email'), dedup, and the no-regress / confidence gates.
"""

from __future__ import annotations

from honestapply.db.models import EmailMessage, Job, JobEvent, Status
from honestapply.db.session import session_scope
from honestapply.email import agent as agent_mod
from honestapply.email import sync as sync_mod


def _runner(messages):
    """A fake agent runner: ignores the prompt, returns a fixed verdict list."""
    def run(_instructions: str) -> dict:
        return {"messages": messages}
    return run


def _msg(msg_id, *, job_id, event_type, confidence, company="Helix AI",
         sender="noreply@greenhouse.io", subject=None, snippet=None):
    return {
        "msg_id": msg_id,
        "thread_id": "t-" + msg_id,
        "sender": sender,
        "subject": subject if subject is not None else f"{event_type} — {company}",
        "snippet": snippet if snippet is not None else f"Regarding your application to {company}.",
        "received_at": "2026-10-01T09:00:00+00:00",
        "job_id": job_id,
        "event_type": event_type,
        "confidence": confidence,
    }


def _applied_job(add_job):
    return add_job(company="Helix AI", title="Senior AI Engineer", status=Status.APPLIED)


def test_interview_email_moves_job_to_interviewing(add_job):
    jid = _applied_job(add_job)
    messages = [
        _msg("m1", job_id=jid, event_type="interview_invite", confidence=92),
        _msg("m2", job_id=None, event_type="other", confidence=0,
             sender="jobs@newsletter.example", subject="Weekly jobs digest",
             snippet="New roles at startups you might like."),
    ]

    counts = sync_mod.run_inbox(agent_runner=_runner(messages), min_confidence=70)

    assert counts == {"scanned": 2, "matched": 1, "updated": 1, "recorded": 2}
    with session_scope() as s:
        assert s.get(Job, jid).status == Status.INTERVIEWING
        # transition attributed to the inbox, not the pipeline
        ev = s.query(JobEvent).filter_by(job_id=jid, to_status=Status.INTERVIEWING).one()
        assert ev.source == "email"
        # both messages recorded; the match carries the job link + classification
        msgs = {m.gmail_msg_id: m for m in s.query(EmailMessage).all()}
        assert set(msgs) == {"m1", "m2"}
        assert msgs["m1"].job_id == jid and msgs["m1"].classification == "interview_invite"
        assert msgs["m2"].job_id is None


def test_dedup_second_run_is_noop(add_job):
    _applied_job(add_job)
    messages = [_msg("m1", job_id=None, event_type="interview_invite", confidence=92)]
    # Resolve job_id lazily so the runner uses the real id.
    with session_scope() as s:
        jid = s.query(Job).one().id
    messages[0]["job_id"] = jid

    first = sync_mod.run_inbox(agent_runner=_runner(messages))
    second = sync_mod.run_inbox(agent_runner=_runner(messages))

    assert first["scanned"] == 1 and first["updated"] == 1
    assert second == {"scanned": 0, "matched": 0, "updated": 0, "recorded": 0}
    with session_scope() as s:
        assert s.query(EmailMessage).count() == 1  # not re-recorded


def test_low_confidence_does_not_move_job(add_job):
    jid = _applied_job(add_job)
    messages = [_msg("m1", job_id=jid, event_type="interview_invite", confidence=40)]

    counts = sync_mod.run_inbox(agent_runner=_runner(messages), min_confidence=70)

    assert counts["updated"] == 0 and counts["matched"] == 0
    with session_scope() as s:
        assert s.get(Job, jid).status == Status.APPLIED  # unchanged
        assert s.query(EmailMessage).count() == 1  # still recorded


def test_company_mention_mismatch_is_dropped(add_job):
    """The agent may pick a job_id; we only trust it if the message actually names
    the company. A high-confidence match whose text is about another company is
    dropped to a no-op (recorded, but unlinked and no status change)."""
    jid = _applied_job(add_job)
    messages = [
        _msg("m1", job_id=jid, event_type="interview_invite", confidence=99,
             subject="Interview — Acme Robotics",
             snippet="We'd like to interview you at Acme Robotics.",
             sender="talent@acme.example"),
    ]

    counts = sync_mod.run_inbox(agent_runner=_runner(messages), min_confidence=70)

    assert counts["updated"] == 0 and counts["matched"] == 0
    with session_scope() as s:
        assert s.get(Job, jid).status == Status.APPLIED
        m = s.query(EmailMessage).one()
        assert m.gmail_msg_id == "m1" and m.job_id is None


def test_no_candidates_short_circuits(add_job):
    """With nothing applied to, the agent is never consulted."""
    add_job(company="Helix AI", title="Senior AI Engineer", status=Status.SCORED)

    called = {"n": 0}

    def runner(_instructions):
        called["n"] += 1
        return {"messages": []}

    counts = sync_mod.run_inbox(agent_runner=runner)
    assert counts == {"scanned": 0, "matched": 0, "updated": 0, "recorded": 0}
    assert called["n"] == 0  # short-circuited before spawning the agent


def test_decide_status_never_regresses_and_terminals_win():
    # progress only moves forward
    assert sync_mod._decide_status(Status.APPLIED, "interview_invite") == Status.INTERVIEWING
    assert sync_mod._decide_status(Status.INTERVIEWING, "application_received") is None
    assert sync_mod._decide_status(Status.INTERVIEWING, "interview_invite") is None  # no-op
    # terminals allowed from any state
    assert sync_mod._decide_status(Status.INTERVIEWING, "rejection") == Status.REJECTED
    assert sync_mod._decide_status(Status.APPLIED, "offer") == Status.OFFER
    # unknown / info_request -> nothing
    assert sync_mod._decide_status(Status.APPLIED, "info_request") is None


def test_company_mention_is_specific():
    norm = sync_mod._normalize_company("Helix AI GmbH")
    assert sync_mod._mentions_company("Your Helix AI application — thanks", norm) is True
    assert sync_mod._mentions_company("Acme Robotics role", norm) is False


def test_agent_parse_result_takes_last_block():
    stdout = (
        "thinking...\n"
        '<<<RESULT>>>{"messages": []}<<<END>>>\n'
        'final: <<<RESULT>>>{"messages": [{"msg_id": "x"}]}<<<END>>>\n'
    )
    data = agent_mod.parse_result(stdout)
    assert data == {"messages": [{"msg_id": "x"}]}


def test_agent_parse_result_raises_without_block():
    import pytest

    with pytest.raises(agent_mod.InboxAgentError):
        agent_mod.parse_result("no result block here")
