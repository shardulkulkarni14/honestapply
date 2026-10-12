"""The lifetime per-company cap is applied BEFORE tailor, not only at apply.

Covers the shared submission counter (single source of truth for the cap) and
the tailor-stage retire helper that drops capped employers before any resume or
cover letter is built.
"""

from __future__ import annotations


def _job(company: str, n: int):
    from honestapply.db.models import Job, url_hash

    url = f"https://x/{company}/{n}"
    return Job(company=company, title="AI Engineer", url=url, url_hash=url_hash(url))


def _status(session, obj):
    from honestapply.db.models import Job

    return session.get(Job, obj.id).status


def test_company_total_submissions_counts_distinct_real_applied():
    from honestapply.db.models import Application
    from honestapply.db.session import session_scope
    from honestapply.stages.track import company_total_submissions

    with session_scope() as s:
        j1, j2, j3 = _job("Acme", 1), _job("Acme", 2), _job("Other", 3)
        s.add_all([j1, j2, j3])
        s.flush()
        # two distinct real+applied roles at Acme (one with a duplicate row)
        s.add_all([
            Application(job_id=j1.id, mode="real", status="applied"),
            Application(job_id=j1.id, mode="real", status="applied"),  # dup -> still 1 role
            Application(job_id=j2.id, mode="real", status="applied"),
            # these must NOT count:
            Application(job_id=j3.id, mode="dry_run", status="dry_run_completed"),
            Application(job_id=j3.id, mode="real", status="needs_human"),
        ])
        s.flush()

        assert company_total_submissions(s, "Acme") == 2
        assert company_total_submissions(s, "  acme ") == 2  # case/space-insensitive
        assert company_total_submissions(s, "Other") == 0  # no real+applied
        assert company_total_submissions(s, "") == 0


def test_retire_capped_employers_drops_only_capped():
    from honestapply.db.models import Application, Status
    from honestapply.db.session import session_scope
    from honestapply.stages.tailor import _retire_capped_employers

    with session_scope() as s:
        capped = _job("Acme", 1)       # Acme already at the cap via j_a/j_b below
        fresh = _job("Newco", 2)       # Newco has no prior submissions
        j_a, j_b = _job("Acme", 9), _job("Acme", 10)
        s.add_all([capped, fresh, j_a, j_b])
        s.flush()
        capped.status = Status.SCORED
        fresh.status = Status.SCORED
        s.add_all([
            Application(job_id=j_a.id, mode="real", status="applied"),
            Application(job_id=j_b.id, mode="real", status="applied"),
        ])  # Acme now has 2 distinct real applications == total_cap
        s.flush()

        kept = _retire_capped_employers(s, [capped.id, fresh.id], total_cap=2)
        s.flush()

        assert kept == [fresh.id]
        assert _status(s, capped) == Status.SKIPPED_COMPANY_CAP
        assert _status(s, fresh) == Status.SCORED  # untouched


def test_retire_capped_employers_disabled_keeps_all():
    from honestapply.db.session import session_scope
    from honestapply.stages.tailor import _retire_capped_employers

    with session_scope() as s:
        j = _job("Acme", 1)
        s.add(j)
        s.flush()
        # total_cap <= 0 disables the check: every id is kept
        assert _retire_capped_employers(s, [j.id], total_cap=0) == [j.id]
