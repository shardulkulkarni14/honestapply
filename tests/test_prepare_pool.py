"""The parallel prepare pool: correctness under concurrency, not speed.

Every test runs offline on the stub provider (or a small fake wrapped around
it) against the per-test SQLite file, with a résumé copied from the tracked
example so nothing here depends on a user's gitignored data.
"""

from __future__ import annotations

import inspect
import re
import shutil
import threading
import time
from pathlib import Path

import pytest

from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.llm.base import LLMError, LLMProvider, StubProvider

ALLOWED_PREPARE_WRITES = {
    Status.ENRICHED,
    Status.SCORED,
    Status.SKIPPED_LOW_FIT,
    Status.TAILORED,
    Status.COVERED,
    Status.NEEDS_HUMAN,
    Status.FAILED,
}

EXPECTED_CHAIN = [
    (None, Status.ENRICHED),
    (Status.ENRICHED, Status.SCORED),
    (Status.SCORED, Status.TAILORED),
    (Status.TAILORED, Status.COVERED),
]


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------
@pytest.fixture
def resume_dir(tmp_path, monkeypatch):
    """Point PATHS at a temp root holding the example résumé as default.yaml, so
    tailor/cover have a base résumé and PDFs land in the temp dir."""
    from honestapply.config import PATHS

    example = Path(PATHS.root) / "config" / "resume.example.yaml"
    root = tmp_path / "root"
    (root / "data" / "resumes").mkdir(parents=True)
    shutil.copy(example, root / "data" / "resumes" / "default.yaml")
    monkeypatch.setattr(PATHS, "root", root)
    return root / "data" / "resumes"


@pytest.fixture
def seed(add_job):
    """seed(n, company=...) -> ids of n ENRICHED jobs with distinct URLs."""
    counter = {"n": 0}

    def _seed(n: int, **kw) -> list[int]:
        ids = []
        for _ in range(n):
            counter["n"] += 1
            k = counter["n"]
            ids.append(
                add_job(url=f"https://boards.greenhouse.io/acme/jobs/{k}", **kw)
            )
        return ids

    return _seed


def _statuses(ids: list[int]) -> dict[int, str]:
    with session_scope() as s:
        return {jid: s.get(Job, jid).status for jid in ids}


def _events(jid: int) -> list[tuple[str | None, str]]:
    with session_scope() as s:
        job = s.get(Job, jid)
        return [(e.from_status, e.to_status) for e in sorted(job.events, key=lambda e: e.id)]


def _drive(ids, **kw):
    from honestapply.stages.prepare import drive_jobs

    kw.setdefault("handle_signals", False)
    kw.setdefault("pause_seconds", 0.2)
    return drive_jobs(ids, **kw)


class WrappedStub(LLMProvider):
    """A stub with hooks: per-call delay, failures keyed on prompt content."""

    name = "test"

    def __init__(self, *, delay: float = 0.0, fail_when: str | None = None,
                 fail_message: str = "boom", fail_times: int | None = None,
                 on_call=None) -> None:
        super().__init__()
        self._stub = StubProvider()
        self.delay = delay
        self.fail_when = fail_when
        self.fail_message = fail_message
        self.fail_times = fail_times  # None = always when fail_when matches
        self.on_call = on_call
        self.calls = 0
        self.failures = 0
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    def complete(self, prompt, *, system=None, max_tokens=2048, temperature=0.2):
        with self._lock:
            self.calls += 1
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            if self.on_call:
                self.on_call(self)
            if self.delay:
                time.sleep(self.delay)
            if self.fail_when and self.fail_when in prompt:
                with self._lock:
                    should_fail = self.fail_times is None or self.failures < self.fail_times
                    if should_fail:
                        self.failures += 1
                if should_fail:
                    raise LLMError(self.fail_message)
            return self._stub.complete(prompt)
        finally:
            with self._lock:
                self._in_flight -= 1


class NineScorer(WrappedStub):
    """Scores 9 instead of the stub's 8 — distinguishable in the DB."""

    def complete(self, prompt, **kw):
        out = super().complete(prompt, **kw)
        return out.replace('"score": 8', '"score": 9')


# ---------------------------------------------------------------------------
# Happy path + isolation
# ---------------------------------------------------------------------------
def test_all_covered_with_four_workers(resume_dir, seed):
    ids = seed(8)
    provider = WrappedStub(delay=0.05)
    summary = _drive(ids, workers=4, provider=provider)

    assert summary.covered == 8
    assert summary.errors == 0
    assert set(_statuses(ids).values()) == {Status.COVERED}
    assert provider.max_in_flight > 1, "four workers never overlapped an LLM call"
    with session_scope() as s:
        for jid in ids:
            j = s.get(Job, jid)
            assert Path(j.tailored_resume_path).exists()
            assert Path(j.cover_letter_path).exists()
            assert j.score == 8


def test_failure_isolation_leaves_the_failed_job_untouched(resume_dir, seed):
    good = seed(4)
    bad = seed(2, company="Broken Co")
    provider = WrappedStub(fail_when="Broken Co", fail_message="provider exploded")

    summary = _drive(good + bad, workers=3, provider=provider)

    st = _statuses(good + bad)
    assert all(st[j] == Status.COVERED for j in good)
    # A transient error is not a verdict on the job: it stays where it was,
    # not `failed`, so a later run retries it.
    assert all(st[j] == Status.ENRICHED for j in bad)
    assert summary.covered == 4
    assert summary.errors == 2
    for j in bad:
        assert "provider exploded" in summary.outcomes[j].error
        assert _events(j) == [(None, Status.ENRICHED)]  # nothing written


# ---------------------------------------------------------------------------
# Idempotency / compare-and-set
# ---------------------------------------------------------------------------
def test_each_stage_commits_once_even_when_two_drives_race(resume_dir, seed):
    """Two pools driving the SAME ids concurrently (the multi-process case)
    must not double-process a stage: exactly one event per transition."""
    ids = seed(6)
    provider = WrappedStub(delay=0.05)
    results = {}

    def run(tag):
        results[tag] = _drive(ids, workers=3, provider=provider)

    threads = [threading.Thread(target=run, args=(tag,)) for tag in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(_statuses(ids).values()) == {Status.COVERED}
    for jid in ids:
        assert _events(jid) == EXPECTED_CHAIN, f"job {jid} has duplicate/missing transitions"


def test_commit_if_status_rejects_a_stale_write(add_job):
    """The CAS itself: a worker whose job moved on while it worked writes nothing."""
    from honestapply.db.session import commit_if_status

    jid = add_job()
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.status == Status.ENRICHED
        job.score = 8
        job.status = Status.SCORED  # our pending result
        # ...but meanwhile someone else (another worker, the dashboard) moved it
        with session_scope() as other:
            other.get(Job, jid).status = Status.NEEDS_HUMAN
        assert commit_if_status(s, job, Status.ENRICHED) is False

    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == Status.NEEDS_HUMAN
        assert j.score is None  # our write was discarded wholesale
    assert _events(jid) == [(None, Status.ENRICHED), (Status.ENRICHED, Status.NEEDS_HUMAN)]


def test_commit_if_status_lands_when_unchanged(add_job):
    from honestapply.db.session import commit_if_status

    jid = add_job()
    with session_scope() as s:
        job = s.get(Job, jid)
        job.status = Status.SCORED
        job.score = 7
        assert commit_if_status(s, job, Status.ENRICHED) is True
    with session_scope() as s:
        assert s.get(Job, jid).score == 7
    assert _events(jid) == [(None, Status.ENRICHED), (Status.ENRICHED, Status.SCORED)]


def test_per_stage_commits_record_every_transition(resume_dir, seed):
    """Task C: because each stage is its own transaction, the event log now has
    one row per hop rather than only the net change of a whole drive."""
    (jid,) = seed(1)
    _drive([jid], workers=1)
    assert _events(jid) == EXPECTED_CHAIN


# ---------------------------------------------------------------------------
# Target / stop
# ---------------------------------------------------------------------------
def test_target_overshoot_is_bounded_by_the_worker_count(resume_dir, seed):
    ids = seed(12)
    workers, target = 4, 3
    provider = WrappedStub(delay=0.03)
    summary = _drive(ids, workers=workers, target=target, provider=provider)

    covered = sum(1 for st in _statuses(ids).values() if st == Status.COVERED)
    assert summary.target_reached
    assert target <= covered <= target + workers - 1
    assert summary.covered == covered
    # Queued jobs were cancelled, in-flight ones stopped after their stage.
    assert summary.processed < len(ids)


def test_preset_stop_event_does_nothing(resume_dir, seed):
    ids = seed(3)
    stop = threading.Event()
    stop.set()
    summary = _drive(ids, workers=2, stop_event=stop)
    assert summary.stopped
    assert summary.covered == 0
    assert set(_statuses(ids).values()) == {Status.ENRICHED}


def test_stop_event_mid_run_finishes_the_current_stage_only(resume_dir, seed):
    ids = seed(4)
    stop = threading.Event()
    provider = WrappedStub(on_call=lambda p: stop.set())  # stop on the first LLM call

    summary = _drive(ids, workers=1, stop_event=stop, provider=provider)

    st = _statuses(ids)
    assert summary.stopped and not summary.target_reached
    assert summary.covered == 0
    # The first job's score stage completed (its result is kept), nothing ran after.
    assert sorted(st.values()) == sorted([Status.SCORED] + [Status.ENRICHED] * 3)
    assert provider.calls == 1


# ---------------------------------------------------------------------------
# Same result serial vs parallel
# ---------------------------------------------------------------------------
def test_workers_1_and_4_reach_the_same_final_statuses(resume_dir, seed):
    def batch():
        return seed(3) + seed(2, company="Broken Co") + seed(2, description="nothing relevant")

    a, b = batch(), batch()
    provider = WrappedStub(fail_when="Broken Co")
    sa = _drive(a, workers=1, provider=provider)
    sb = _drive(b, workers=4, provider=provider)

    fa, fb = sorted(_statuses(a).values()), sorted(_statuses(b).values())
    assert fa == fb
    # ...and the mix is genuinely mixed, so the comparison means something.
    assert Status.COVERED in fa and Status.ENRICHED in fa and Status.NEEDS_HUMAN in fa
    assert (sa.covered, sa.errors) == (sb.covered, sb.errors)


# ---------------------------------------------------------------------------
# Score-provider override
# ---------------------------------------------------------------------------
def test_score_provider_override_is_used_for_scoring_only(resume_dir, seed):
    ids = seed(3)
    default = WrappedStub()
    scorer = NineScorer()
    _drive(ids, workers=2, provider=default, score_provider=scorer)

    with session_scope() as s:
        assert all(s.get(Job, j).score == 9 for j in ids)
    assert scorer.calls == 3            # one score call per job
    assert default.calls == 3 * 2       # tailor + cover per job, on the default


def test_settings_score_provider_override_resolves_via_get_provider(monkeypatch, add_job):
    """HONESTAPPLY_SCORE_PROVIDER picks the scoring provider even when the
    default provider is something else (here: one that would need a key)."""
    from honestapply.config import get_settings
    from honestapply.stages.score import get_score_provider, run_score

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HONESTAPPLY_SCORE_PROVIDER", "stub")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.llm_provider == "anthropic"
    assert isinstance(get_score_provider(settings), StubProvider)

    jid = add_job()
    assert run_score(workers=2) == 1
    with session_scope() as s:
        assert s.get(Job, jid).status == Status.SCORED


def test_score_provider_defaults_to_the_main_provider(monkeypatch):
    from honestapply.config import Settings
    from honestapply.stages.score import get_score_provider

    s = Settings(_env_file=None, llm_provider="stub")
    assert s.honestapply_score_provider is None
    assert isinstance(get_score_provider(s), StubProvider)


# ---------------------------------------------------------------------------
# Rate-limit pause
# ---------------------------------------------------------------------------
def test_rate_limit_pauses_every_worker_and_leaves_the_job_unmarked(resume_dir, seed):
    ids = seed(4)
    provider = WrappedStub(
        fail_when="JOB_POSTING", fail_message="Claude usage limit reached (429)", fail_times=1,
    )
    t0 = time.monotonic()
    summary = _drive(ids, workers=2, provider=provider, pause_seconds=0.4)
    elapsed = time.monotonic() - t0

    assert summary.rate_limit_pauses == 1
    assert elapsed >= 0.4, "workers did not wait out the pause"
    st = _statuses(ids)
    limited = [j for j in ids if summary.outcomes[j].error]
    assert len(limited) == 1
    assert st[limited[0]] == Status.ENRICHED  # not failed — retried next run
    assert all(st[j] == Status.COVERED for j in ids if j != limited[0])


def test_rate_limit_detection_is_narrow():
    from honestapply.stages._workers import is_rate_limit

    assert is_rate_limit(LLMError("HTTP 429 Too Many Requests"))
    assert is_rate_limit(LLMError("You've hit your usage limit"))
    assert is_rate_limit(LLMError("overloaded_error"))
    assert not is_rate_limit(LLMError("claude CLI failed (exit 1): bad prompt"))
    assert not is_rate_limit(RuntimeError("429"))  # only LLM errors count


def test_stage_runner_marks_failed_but_never_for_rate_limits(add_job):
    from honestapply.stages.score import score_job

    jid_fail = add_job(company="Broken Co")
    jid_limit = add_job(company="Limited Co", url="https://boards.greenhouse.io/acme/jobs/2")
    p = WrappedStub(fail_when="Broken Co")
    assert score_job(jid_fail, provider=p, profile_summary="", threshold=7) == Status.FAILED
    p2 = WrappedStub(fail_when="Limited Co", fail_message="rate limit exceeded")
    with pytest.raises(LLMError):
        score_job(jid_limit, provider=p2, profile_summary="", threshold=7)
    st = _statuses([jid_fail, jid_limit])
    assert st[jid_fail] == Status.FAILED
    assert st[jid_limit] == Status.ENRICHED


# ---------------------------------------------------------------------------
# Lock-retry replay
# ---------------------------------------------------------------------------
def test_commit_retry_replays_pending_updates_after_a_rollback(add_job):
    """The pieces the locked-commit retry is built from: capture, rollback,
    replay — and the replayed write still logs the right from/to pair."""
    from honestapply.db.session import _replay_pending, _snapshot_pending

    jid = add_job()
    with session_scope() as s:
        job = s.get(Job, jid)
        job.status = Status.SCORED
        job.score = 6
        pending = _snapshot_pending(s)
        assert pending and pending[0][1] == {"status": Status.SCORED, "score": 6}
        s.rollback()  # what a failed flush leaves behind
        assert job.status == Status.ENRICHED  # expired + reloaded: changes gone
        _replay_pending(s, pending)
    with session_scope() as s:
        j = s.get(Job, jid)
        assert (j.status, j.score) == (Status.SCORED, 6)
    assert _events(jid) == [(None, Status.ENRICHED), (Status.ENRICHED, Status.SCORED)]


def test_commit_retry_refuses_to_replay_new_rows():
    from honestapply.db.session import _snapshot_pending

    with session_scope() as s:
        s.add(Job(company="x", title="y", url="u", url_hash="h"))
        assert _snapshot_pending(s) is None


# ---------------------------------------------------------------------------
# Safety contract
# ---------------------------------------------------------------------------
def test_prepare_only_ever_writes_prepare_statuses(resume_dir, seed):
    from honestapply.stages import prepare

    # Static: every status the module names is a prepare-stage status (plus
    # DISCOVERED, which is only ever read as a precondition).
    src = inspect.getsource(prepare)
    named = {getattr(Status, m) for m in re.findall(r"Status\.([A-Z_]+)", src)}
    assert named <= ALLOWED_PREPARE_WRITES | {Status.DISCOVERED}
    assert {s.produces for s in prepare.STAGES} <= ALLOWED_PREPARE_WRITES
    for forbidden in ("APPLIED", "READY_TO_APPLY", "DRY_RUN_COMPLETED", "run_apply"):
        assert forbidden not in src

    # Dynamic: a mixed drive writes only those statuses into the event log.
    ids = seed(3) + seed(1, company="Broken Co") + seed(1, description="nothing relevant")
    _drive(ids, workers=3, provider=WrappedStub(fail_when="Broken Co"))
    written = {to for j in ids for _, to in _events(j)[1:]}
    assert written and written <= ALLOWED_PREPARE_WRITES


def test_apply_stage_is_not_parallel():
    from honestapply.stages import apply as apply_stage

    assert not hasattr(apply_stage, "workers")
    assert "workers" not in inspect.signature(apply_stage.run_apply).parameters
    src = inspect.getsource(apply_stage)
    for token in ("ThreadPoolExecutor", "run_pool", "drive_jobs", "stages.prepare", "_workers"):
        assert token not in src, f"apply must stay a single serialised consumer ({token})"


def test_prepare_workers_ships_at_one():
    """Concurrency is opt-in: the default must be the familiar serial behaviour."""
    from honestapply.config import Settings

    s = Settings(_env_file=None)
    assert s.honestapply_prepare_workers == 1
    assert s.honestapply_score_provider is None
    assert s.honestapply_llm_pause_seconds == 120


# ---------------------------------------------------------------------------
# Stage runners with workers > 1
# ---------------------------------------------------------------------------
def test_stage_runners_accept_workers(resume_dir, seed):
    from honestapply.stages.cover_letter import run_cover_letters
    from honestapply.stages.score import run_score
    from honestapply.stages.tailor import run_tailor

    ids = seed(5)
    assert run_score(workers=3) == 5
    assert run_tailor(workers=3) == 5
    assert run_cover_letters(workers=3) == 5
    assert set(_statuses(ids).values()) == {Status.COVERED}
    for jid in ids:
        assert _events(jid) == EXPECTED_CHAIN


def test_batch_drive_script_prints_the_covered_line(tmp_path, monkeypatch):
    """The shell loops grep this exact wording; the script must keep it."""
    import subprocess
    import sys

    from honestapply.config import PATHS

    root = Path(PATHS.root)
    if not (root / "data" / "resumes" / "default.yaml").exists():
        pytest.skip("repo has no data/resumes/default.yaml (run `honestapply init`)")
    from honestapply.db.models import url_hash

    with session_scope() as s:
        j = Job(
            company="Helix AI", title="AI Engineer", url="https://boards.greenhouse.io/x/1",
            url_hash=url_hash("https://boards.greenhouse.io/x/1"), status=Status.ENRICHED,
            description="Python, FastAPI, PostgreSQL, Docker, AWS backend microservices.",
        )
        s.add(j)
        s.flush()
        jid = j.id
    out = subprocess.run(
        [sys.executable, str(root / "scripts" / "batch_drive.py"), "--ids", str(jid), "--workers", "2"],
        capture_output=True, text=True, cwd=root, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-500:]
    assert "COVERED (score=8)" in out.stdout
    assert "batch_drive done: 1 newly COVERED" in out.stdout
