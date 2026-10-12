"""The needs_human report lists only active needs_human jobs, with reasons."""

from __future__ import annotations


def _job(company, status, reason="", archived=False):
    from datetime import datetime, timezone

    from honestapply.db.models import Job, Status, url_hash

    url = f"https://x/{company}/{status}"
    j = Job(company=company, title="AI Engineer", url=url, url_hash=url_hash(url),
            status=status, status_reason=reason)
    if archived:
        j.archived_at = datetime.now(timezone.utc)
    _ = Status  # imported for callers/readability
    return j


def test_needs_human_jobs_filters_and_reports_reasons():
    from honestapply.db.models import Status
    from honestapply.db.session import session_scope
    from honestapply.stages.track import needs_human_jobs

    with session_scope() as s:
        s.add_all([
            _job("Acme", Status.NEEDS_HUMAN, reason="CAPTCHA blocked submit"),
            _job("Beta", Status.NEEDS_HUMAN, reason="required PAN card field"),
            _job("Gone", Status.NEEDS_HUMAN, reason="x", archived=True),  # excluded
            _job("Other", Status.APPLIED, reason="n/a"),                   # excluded
        ])
        s.flush()

    rows = needs_human_jobs()
    companies = {r["company"] for r in rows}
    assert companies == {"Acme", "Beta"}  # archived + non-needs_human excluded
    reasons = {r["company"]: r["reason"] for r in rows}
    assert reasons["Acme"] == "CAPTCHA blocked submit"
    assert all(r["url"] for r in rows)


def test_needs_human_jobs_respects_limit():
    from honestapply.db.models import Status
    from honestapply.db.session import session_scope
    from honestapply.stages.track import needs_human_jobs

    with session_scope() as s:
        s.add_all([_job(f"Co{i}", Status.NEEDS_HUMAN, reason="r") for i in range(5)])
        s.flush()

    assert len(needs_human_jobs(limit=2)) == 2
    assert len(needs_human_jobs()) == 5
