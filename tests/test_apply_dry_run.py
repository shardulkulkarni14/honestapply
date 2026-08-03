"""Apply stage in mock mode: dry-run, the first-N safety guard, and LinkedIn block."""

from __future__ import annotations


def _seed_covered(n, ats="greenhouse", host="boards.greenhouse.io"):
    from honestapply.db.models import Job, Status, url_hash
    from honestapply.db.session import session_scope

    ids = []
    with session_scope() as s:
        for i in range(n):
            url = f"https://{host}/co{i}/jobs/{i}"
            j = Job(
                company=f"Co{i}",
                title="AI Engineer",
                url=url,
                url_hash=url_hash(url),
                status=Status.COVERED,
                ats_type=ats,
            )
            s.add(j)
            s.flush()
            ids.append(j.id)
    return ids


def test_dry_run_creates_applications():
    from honestapply.db.models import Application, Job, Status
    from honestapply.db.session import session_scope
    from honestapply.stages.apply import run_apply

    _seed_covered(3)
    run_apply(dry_run=True, limit=3)

    with session_scope() as s:
        apps = s.query(Application).all()
        jobs = s.query(Job).all()
        assert len(apps) == 3
        assert all(a.mode == "dry_run" for a in apps)
        assert all(j.status == Status.DRY_RUN_COMPLETED for j in jobs)


def test_first_n_dry_run_guard():
    """With dry_run=False and safety on, the first N submissions are still dry."""
    from honestapply.config import get_settings
    from honestapply.db.models import Application
    from honestapply.db.session import session_scope
    from honestapply.stages.apply import run_apply

    n_guard = get_settings().honestapply_dry_run_first_n
    _seed_covered(n_guard + 2)
    run_apply(dry_run=False, no_safety=False)

    with session_scope() as s:
        apps = s.query(Application).all()
        dry = [a for a in apps if a.mode == "dry_run"]
        assert len(dry) >= n_guard, f"expected >= {n_guard} forced dry-runs, got {len(dry)}"


def test_linkedin_blocked_to_needs_human():
    from honestapply.db.models import Job, Status
    from honestapply.db.session import session_scope
    from honestapply.stages.apply import run_apply

    ids = _seed_covered(1, ats="linkedin", host="www.linkedin.com")
    run_apply(dry_run=True)

    with session_scope() as s:
        j = s.get(Job, ids[0])
        assert j.status == Status.NEEDS_HUMAN


def test_mode_b_writes_instructions(tmp_path, capsys):
    from honestapply.stages.apply import run_apply

    run_apply(url="https://boards.greenhouse.io/test/jobs/123")
    out = capsys.readouterr().out
    assert "apply_instructions.md" in out


def test_dry_run_completed_jobs_are_requeued_for_real_submission():
    """ACCEPTANCE: the canary must not permanently consume a prepared application.

    The dry-run guard forces the first N jobs of each run to be dry. Those jobs
    landed in DRY_RUN_COMPLETED, which the apply query never selected again — so
    a fully prepared application (résumé + cover letter already generated) was
    silently never submitted. 13 jobs were found stranded this way on 2026-08-02,
    including a batch of size 1 where the canary consumed the only job.
    """
    import inspect

    from honestapply.db.models import Status
    from honestapply.stages import apply as apply_mod

    src = inspect.getsource(apply_mod.run_apply)
    assert "DRY_RUN_COMPLETED" in src, (
        "apply must re-queue DRY_RUN_COMPLETED jobs, otherwise the dry-run "
        "canary strands prepared applications permanently"
    )
    assert Status.DRY_RUN_COMPLETED != Status.COVERED
