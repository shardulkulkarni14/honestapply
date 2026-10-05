"""Offline tests for the Gmail inbox sync.

No network, no Google libs: the Gmail fetch functions are monkeypatched to serve
canned messages, and a fake classifier returns deterministic verdicts. Exercises
matching, the status transition (attributed to source='email'), dedup, and the
no-regress / confidence guards.
"""

from __future__ import annotations

import re

from honestapply.db.models import EmailMessage, Job, JobEvent, Status
from honestapply.db.session import session_scope
from honestapply.email import sync as sync_mod
from honestapply.email.gmail import ParsedEmail


class FakeProvider:
    """Duck-typed LLM: reads the candidate id out of the prompt and calls an
    email an interview invite iff the word 'interview' is in it."""

    def complete_json(self, prompt, **_kw):
        m = re.search(r"id=(\d+)", prompt)
        jid = int(m.group(1)) if m else None
        if jid and "interview" in prompt.lower():
            return {"job_id": jid, "event_type": "interview_invite", "confidence": 92}
        return {"job_id": None, "event_type": "other", "confidence": 0}


def _email(msg_id, *, subject, body, sender="noreply@greenhouse.io"):
    domain = sender.split("@", 1)[1]
    return ParsedEmail(
        msg_id=msg_id,
        thread_id="t-" + msg_id,
        sender=sender,
        sender_domain=domain,
        subject=subject,
        snippet=body[:80],
        body=body,
        received_at=None,
    )


def _patch_gmail(monkeypatch, emails: dict[str, ParsedEmail]):
    monkeypatch.setattr(sync_mod, "list_recent_ids", lambda *a, **k: list(emails.keys()))
    monkeypatch.setattr(sync_mod, "get_message", lambda _svc, mid: emails[mid])


def _applied_job(add_job):
    jid = add_job(company="Helix AI", title="Senior AI Engineer", status=Status.APPLIED)
    return jid


def test_interview_email_moves_job_to_interviewing(monkeypatch, add_job):
    jid = _applied_job(add_job)
    emails = {
        "m1": _email(
            "m1",
            subject="Interview invitation — Helix AI",
            body="Hi, we'd love to schedule an interview for the Senior AI Engineer role at Helix AI.",
        ),
        "m2": _email(
            "m2",
            subject="Weekly jobs digest",
            body="New roles at startups you might like. Unrelated newsletter.",
            sender="jobs@newsletter.example",
        ),
    }
    _patch_gmail(monkeypatch, emails)

    counts = sync_mod.run_inbox(service=object(), provider=FakeProvider(), min_confidence=70)

    assert counts == {"scanned": 2, "matched": 1, "updated": 1, "recorded": 2}
    with session_scope() as s:
        assert s.get(Job, jid).status == Status.INTERVIEWING
        # transition attributed to the inbox, not the pipeline
        ev = s.query(JobEvent).filter_by(job_id=jid, to_status=Status.INTERVIEWING).one()
        assert ev.source == "email"
        # both emails recorded; the match carries the job link + classification
        msgs = {m.gmail_msg_id: m for m in s.query(EmailMessage).all()}
        assert set(msgs) == {"m1", "m2"}
        assert msgs["m1"].job_id == jid and msgs["m1"].classification == "interview_invite"
        assert msgs["m2"].job_id is None


def test_dedup_second_run_is_noop(monkeypatch, add_job):
    _applied_job(add_job)
    emails = {
        "m1": _email("m1", subject="Interview — Helix AI", body="interview for Helix AI role"),
    }
    _patch_gmail(monkeypatch, emails)

    first = sync_mod.run_inbox(service=object(), provider=FakeProvider())
    second = sync_mod.run_inbox(service=object(), provider=FakeProvider())

    assert first["scanned"] == 1 and first["updated"] == 1
    assert second == {"scanned": 0, "matched": 0, "updated": 0, "recorded": 0}
    with session_scope() as s:
        assert s.query(EmailMessage).count() == 1  # not re-recorded


def test_low_confidence_does_not_move_job(monkeypatch, add_job):
    jid = _applied_job(add_job)
    emails = {"m1": _email("m1", subject="Interview — Helix AI", body="interview at Helix AI")}
    _patch_gmail(monkeypatch, emails)

    class LowConf(FakeProvider):
        def complete_json(self, prompt, **_kw):
            out = super().complete_json(prompt)
            out["confidence"] = 40
            return out

    counts = sync_mod.run_inbox(service=object(), provider=LowConf(), min_confidence=70)
    assert counts["updated"] == 0
    with session_scope() as s:
        assert s.get(Job, jid).status == Status.APPLIED  # unchanged
        assert s.query(EmailMessage).count() == 1  # still recorded


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


def test_company_match_is_specific():
    e = _email("x", subject="Your Helix AI application", body="thanks for applying to Helix AI")
    assert sync_mod._email_mentions(e, sync_mod._normalize_company("Helix AI GmbH")) is True
    assert sync_mod._email_mentions(e, sync_mod._normalize_company("Acme Robotics")) is False
