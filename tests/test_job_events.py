"""The before_flush listener records every Job status change into job_events."""

from __future__ import annotations

from honestapply.db.events import transition
from honestapply.db.models import Job, JobEvent, Status
from honestapply.db.session import session_scope


def _events(job_id: int) -> list[JobEvent]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        return sorted(job.events, key=lambda e: e.at or e.id)


def test_creating_a_job_records_its_first_event(add_job):
    jid = add_job(status=Status.DISCOVERED)
    evs = _events(jid)
    assert len(evs) == 1
    assert evs[0].from_status is None
    assert evs[0].to_status == Status.DISCOVERED
    assert evs[0].source == "pipeline"


def test_job_created_without_explicit_status_uses_the_column_default():
    """A Job added bare relies on the status column default; the first event must
    still record that default, not a NULL (column defaults aren't applied until
    the INSERT, after the listener runs)."""
    jid_holder = {}
    with session_scope() as s:
        j = Job(company="Acme", title="AI Engineer", url="u", url_hash="h")
        s.add(j)
        s.flush()
        jid_holder["id"] = j.id

    evs = _events(jid_holder["id"])
    assert len(evs) == 1
    assert evs[0].from_status is None
    assert evs[0].to_status == Status.DISCOVERED  # the column default


def test_status_change_is_logged_with_from_and_to(add_job):
    jid = add_job(status=Status.SCORED)
    with session_scope() as s:
        s.get(Job, jid).status = Status.TAILORED

    evs = _events(jid)
    assert [(e.from_status, e.to_status) for e in evs] == [
        (None, Status.SCORED),
        (Status.SCORED, Status.TAILORED),
    ]


def test_no_event_when_status_is_unchanged(add_job):
    """Touching other fields must not fabricate a transition."""
    jid = add_job(status=Status.SCORED)
    with session_scope() as s:
        s.get(Job, jid).notes = "a note, but no status change"

    assert len(_events(jid)) == 1  # just the creation event


def test_transition_context_attributes_source_and_note(add_job):
    jid = add_job(status=Status.APPLIED)
    with transition("dashboard", note="recruiter replied"):
        with session_scope() as s:
            s.get(Job, jid).status = Status.INTERVIEWING

    last = _events(jid)[-1]
    assert last.to_status == Status.INTERVIEWING
    assert last.source == "dashboard"
    assert last.note == "recruiter replied"


def test_source_defaults_back_to_pipeline_after_context(add_job):
    jid = add_job(status=Status.APPLIED)
    with transition("dashboard"):
        with session_scope() as s:
            s.get(Job, jid).status = Status.SCREENING
    # Outside the context, a later change is pipeline again.
    with session_scope() as s:
        s.get(Job, jid).status = Status.REJECTED

    sources = [e.source for e in _events(jid)]
    assert sources[-2:] == ["dashboard", "pipeline"]
