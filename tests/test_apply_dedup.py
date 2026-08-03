"""The dedup guard must only treat a real, completed submission as blocking."""

from __future__ import annotations


def test_has_real_submission_only_for_real_applied():
    from honestapply.db.models import Application, Job, url_hash
    from honestapply.db.session import session_scope
    from honestapply.stages.apply import _has_real_submission

    with session_scope() as s:
        j_dry = Job(company="Acme", title="AI Engineer", url="https://x/1", url_hash=url_hash("https://x/1"))
        j_real = Job(company="Acme", title="ML Engineer", url="https://x/2", url_hash=url_hash("https://x/2"))
        s.add_all([j_dry, j_real])
        s.flush()
        # a dry-run + a needs_human row must NOT block a later real apply
        s.add(Application(job_id=j_dry.id, mode="dry_run", status="dry_run_completed"))
        s.add(Application(job_id=j_dry.id, mode="real", status="needs_human"))
        # a real, applied submission DOES block
        s.add(Application(job_id=j_real.id, mode="real", status="applied"))
        s.flush()

        assert _has_real_submission(s, j_dry.id) is False
        assert _has_real_submission(s, j_real.id) is True
