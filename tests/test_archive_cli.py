"""Archiving removes a job from status counts and analytics, via CLI, reversibly."""

from __future__ import annotations

from honestapply.db.models import Job, JobEvent, Status, utcnow
from honestapply.db.session import session_scope


def _run(args):
    from typer.testing import CliRunner

    from honestapply.cli import app

    return CliRunner().invoke(app, args)


def test_cli_archive_hides_from_status_counts_and_unarchive_restores(add_job):
    from honestapply.stages.track import status_counts

    jid = add_job(status=Status.APPLIED)
    base = status_counts().get("applied", 0)
    assert base >= 1

    assert _run(["archive", str(jid)]).exit_code == 0
    assert status_counts().get("applied", 0) == base - 1
    with session_scope() as s:
        assert s.get(Job, jid).archived_at is not None

    assert _run(["unarchive", str(jid)]).exit_code == 0
    assert status_counts().get("applied", 0) == base
    with session_scope() as s:
        assert s.get(Job, jid).archived_at is None


def test_cli_archive_unknown_job_errors():
    assert _run(["archive", "999999"]).exit_code == 1
    assert _run(["unarchive", "999999"]).exit_code == 1


def test_cli_archive_twice_is_a_noop(add_job):
    jid = add_job(status=Status.APPLIED)
    assert _run(["archive", str(jid)]).exit_code == 0
    res = _run(["archive", str(jid)])
    assert res.exit_code == 0
    assert "already archived" in res.output.lower()


def test_analytics_excludes_archived_jobs(add_job):
    from honestapply.analytics import compute

    jid = add_job(status=Status.APPLIED)
    with session_scope() as s:
        s.add(
            JobEvent(job_id=jid, from_status="", to_status=Status.APPLIED, source="cli", at=utcnow())
        )

    with session_scope() as s:
        before = compute(s)
    assert before["funnel"].get("applied", 0) >= 1

    assert _run(["archive", str(jid)]).exit_code == 0

    with session_scope() as s:
        after = compute(s)
    assert after["funnel"].get("applied", 0) == before["funnel"].get("applied", 0) - 1
    assert after["applied_total"] == before["applied_total"] - 1
