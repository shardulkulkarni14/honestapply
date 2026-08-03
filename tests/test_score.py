"""Scoring stage: stub scoring + the min-score gate."""

from __future__ import annotations


def test_score_marks_scored(add_job):
    from honestapply.db.models import Job, Status
    from honestapply.db.session import session_scope
    from honestapply.stages.score import run_score

    jid = add_job()
    n = run_score()
    assert n == 1
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == Status.SCORED
        assert j.score == 8  # StubProvider returns 8
        assert j.score_reasoning


def test_score_gate_skips_low_fit(add_job):
    """A threshold above the stub score routes the job to skipped_low_fit."""
    from honestapply.db.models import Job, Status
    from honestapply.db.session import session_scope
    from honestapply.stages.score import run_score

    jid = add_job()
    run_score(min_score=9)  # stub returns 8 < 9
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == Status.SKIPPED_LOW_FIT
