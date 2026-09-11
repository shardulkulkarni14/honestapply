"""Prepare pipeline: drive a curated set of jobs enrich → score → tailor →
cover-letter, several jobs at a time.

This is the engine behind ``scripts/batch_drive.py``. It operates strictly on
the ids it is given — never the rest of the backlog — and each job advances as
far as it can: one that fails the score gate, or routes to ``needs_human``,
simply stops there.

Concurrency model (see :mod:`honestapply.stages._workers` for the rules):

* a ``ThreadPoolExecutor`` of *workers* threads; each thread drives one job
  through its stages in order, so a job never runs two stages at once;
* every stage is its own short transaction with a compare-and-set commit, so
  the DB is the only queue and the same job can't be advanced twice;
* the LLM call happens with no lock held; ``claude -p`` releases the GIL while
  the subprocess runs, so threads (not processes) are enough;
* one rate-limit error pauses *every* worker before its next LLM stage, and a
  usage limit that persists across ``llm_pause_give_up_after`` pauses aborts the
  drive instead of sleeping before every remaining job;
* SIGINT/SIGTERM asks in-flight jobs to stop after their current stage; a second
  SIGINT also cancels everything still queued (in-flight ``claude -p`` calls
  still finish or hit their timeout — a signal can't interrupt a subprocess);
* once *target* jobs are COVERED, queued jobs are cancelled and in-flight ones
  stop after their current stage, so the overshoot is bounded by the worker
  count.

Only the prepare stages live here. The apply stage is a single serialised
consumer and is never driven from this module.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from honestapply.config import PATHS, get_settings, load_profile
from honestapply.db.models import Job, Status
from honestapply.db.session import init_db, session_scope
from honestapply.llm.base import LLMProvider, get_provider
from honestapply.logging_setup import get_logger
from honestapply.resume.schema import Resume, list_resumes
from honestapply.stages._workers import (
    PAUSE,
    LLMPause,
    is_rate_limit,
    resolve_workers,
    run_stage_on_job,
)
from honestapply.stages.cover_letter import _cover_one
from honestapply.stages.enrich import _enrich_job
from honestapply.stages.score import _score_one, get_score_provider
from honestapply.stages.tailor import _tailor_one

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Stage table
# ---------------------------------------------------------------------------
@dataclass
class DriveContext:
    """Everything a worker needs, built once per drive and shared read-only."""

    provider: LLMProvider
    score_provider: LLMProvider
    profile_summary: str
    threshold: int
    resumes: list[Resume]
    pause: LLMPause
    stop: threading.Event
    emit: Callable[[str], None]
    target: int = 0
    covered: int = 0  # only the coordinating thread updates this
    target_reached: bool = False
    stopped_by_signal: bool = False


@dataclass(frozen=True)
class StageSpec:
    name: str
    expects: str  # the status a job must be in for this stage to apply
    produces: str  # the status that means "advance to the next stage"
    uses_llm: bool
    run: Callable[[Job, DriveContext], None]


STAGES: tuple[StageSpec, ...] = (
    StageSpec("enrich", Status.DISCOVERED, Status.ENRICHED, False,
              lambda job, ctx: _enrich_job(job)),
    StageSpec("score", Status.ENRICHED, Status.SCORED, True,
              lambda job, ctx: _score_one(job, ctx.score_provider, ctx.profile_summary,
                                          ctx.threshold)),
    StageSpec("tailor", Status.SCORED, Status.TAILORED, True,
              lambda job, ctx: _tailor_one(job, ctx.provider, ctx.resumes)),
    StageSpec("cover", Status.TAILORED, Status.COVERED, True,
              lambda job, ctx: _cover_one(job, ctx.provider, ctx.resumes)),
)
STAGE_NAMES: tuple[str, ...] = tuple(s.name for s in STAGES)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class JobOutcome:
    job_id: int
    tag: str
    status: str | None = None  # final status (None = job not found)
    score: int | None = None
    reason: str | None = None
    ran: list[str] = field(default_factory=list)  # stages that actually applied
    last_stage: str | None = None  # the stage the job stopped at
    error: str | None = None  # exception text, if a stage raised
    skipped: bool = False  # never started: the drive was stopped first

    @property
    def covered(self) -> bool:
        return self.status == Status.COVERED


@dataclass
class DriveSummary:
    covered: int = 0
    processed: int = 0
    errors: int = 0
    rate_limit_pauses: int = 0
    target_reached: bool = False
    stopped: bool = False  # stopped early by signal / stop_event
    outcomes: dict[int, JobOutcome] = field(default_factory=dict)


def format_outcome(o: JobOutcome, covered_total: int) -> str:
    """The one-line report per job — the wording ``scripts/batch_drive.py`` has
    always printed. Shell loops grep ``COVERED (score``; keep that exact."""
    if o.status is None:
        return f"[{o.job_id}] not found"
    if o.error:
        return f"[{o.job_id}] EXCEPTION: {o.error}"
    if o.covered:
        return f"{o.tag} -> COVERED (score={o.score})  [{covered_total} total]"
    if o.last_stage == "enrich" and o.status != Status.ENRICHED:
        return f"{o.tag} -> enrich failed ({o.status})"
    if o.last_stage == "score" and o.status != Status.SCORED:
        return f"{o.tag} -> score={o.score} {o.status}"
    if o.last_stage == "tailor" and o.status != Status.TAILORED:
        return f"{o.tag} -> tailor: {o.status} ({o.reason})"
    return f"{o.tag} -> {o.status} ({o.reason})"


# ---------------------------------------------------------------------------
# Per-job driver (runs on a worker thread)
# ---------------------------------------------------------------------------
def _peek(jid: int) -> tuple[str, str, str, int | None, str | None] | None:
    """(company, title, status, score, status_reason) in one short read."""
    with session_scope() as s:
        job = s.get(Job, jid)
        if job is None:
            return None
        return job.company or "", job.title or "", job.status, job.score, job.status_reason


def _drive_one(jid: int, ctx: DriveContext, stages: Sequence[StageSpec] = STAGES) -> JobOutcome:
    """Advance one job through *stages*, each in its own transaction.

    A stage that doesn't apply (the job is already past it, or another worker
    took it) is skipped; a stage whose result isn't the next status (low fit,
    needs_human, enrich found nothing) ends the chain. Exceptions end the chain
    with the job left exactly as it was — a transient error is not a verdict on
    the job.
    """
    if ctx.stop.is_set():
        return JobOutcome(jid, tag=f"[{jid}]", skipped=True)
    peek = _peek(jid)
    if peek is None:
        return JobOutcome(jid, tag=f"[{jid}]")
    company, title, _, _, _ = peek
    outcome = JobOutcome(jid, tag=f"[{jid}] {company} / {title[:45]}")

    for stage in stages:
        if ctx.stop.is_set():
            break
        if stage.uses_llm:
            # A persistent usage limit should abort the whole drive, not pause
            # 120s before every one of the remaining queued jobs.
            if ctx.pause.should_give_up():
                ctx.stop.set()
                log.warning("prepare.give_up", consecutive=ctx.pause.consecutive)
                break
            ctx.pause.wait(ctx.stop)
            if ctx.stop.is_set():
                break
        try:
            new_status = run_stage_on_job(
                jid,
                stage=stage.name,
                expects=stage.expects,
                work=lambda job, st=stage: st.run(job, ctx),
                mark_failed=False,
            )
        except Exception as exc:  # noqa: BLE001 — isolate this job; status is untouched
            if is_rate_limit(exc):
                ctx.pause.trigger(str(exc))
            log.warning("prepare.stage_error", job_id=jid, stage=stage.name, error=str(exc))
            outcome.error = f"{stage.name}: {exc}"
            outcome.last_stage = stage.name
            break
        if stage.uses_llm:
            ctx.pause.note_progress()  # an LLM call went through → limit has cleared
        if new_status is None:
            continue  # not at this stage (already past it, or raced) — try the next
        outcome.ran.append(stage.name)
        outcome.last_stage = stage.name
        if new_status != stage.produces:
            break  # gated out; the status says why

    final = _peek(jid)
    if final is not None:
        _, _, outcome.status, outcome.score, outcome.reason = final
    return outcome


# ---------------------------------------------------------------------------
# Pool driver
# ---------------------------------------------------------------------------
def _select_stages(names: Iterable[str]) -> tuple[StageSpec, ...]:
    wanted = set(names)
    unknown = wanted - set(STAGE_NAMES)
    if unknown:
        raise ValueError(f"unknown prepare stage(s): {sorted(unknown)}; valid: {STAGE_NAMES}")
    return tuple(s for s in STAGES if s.name in wanted)


def drive_jobs(
    ids: Iterable[int],
    *,
    workers: int | None = None,
    target: int = 0,
    min_score: int | None = None,
    stages: Iterable[str] = STAGE_NAMES,
    stop_event: threading.Event | None = None,
    emit: Callable[[str], None] | None = None,
    pause_seconds: float | None = None,
    provider: LLMProvider | None = None,
    score_provider: LLMProvider | None = None,
    handle_signals: bool = True,
) -> DriveSummary:
    """Drive *ids* through the prepare stages on *workers* threads.

    *target* > 0 stops once that many jobs are COVERED. *stop_event* lets a
    caller stop the drive early; SIGINT/SIGTERM set it too when
    *handle_signals* is on (main thread only). *emit* receives the per-job
    report lines (default: the log). *provider* / *score_provider* /
    *pause_seconds* exist so tests can inject fakes; production resolves them
    from settings.
    """
    id_list = list(dict.fromkeys(int(i) for i in ids))
    stage_specs = _select_stages(stages)
    n_workers = resolve_workers(workers)
    settings = get_settings()
    init_db()

    provider = provider or get_provider(settings)
    if score_provider is None:
        # Score-only override (HONESTAPPLY_SCORE_PROVIDER); otherwise scoring
        # shares the default provider rather than instantiating a second one.
        score_provider = (
            get_score_provider(settings) if settings.honestapply_score_provider else provider
        )
    ctx = DriveContext(
        provider=provider,
        score_provider=score_provider,
        profile_summary=load_profile().summary_for_scoring(),
        threshold=min_score if min_score is not None else settings.honestapply_min_score,
        resumes=list_resumes(PATHS.resumes_dir),
        pause=LLMPause(pause_seconds) if pause_seconds is not None else PAUSE,
        stop=stop_event or threading.Event(),
        emit=emit or (lambda line: log.info("prepare.job", line=line)),
        target=max(0, int(target)),
    )
    summary = DriveSummary()
    pauses_before = ctx.pause.triggers

    _pool_ref: dict[str, ThreadPoolExecutor] = {}

    def on_signal(signum, _frame):
        if ctx.stop.is_set():
            # Second signal: drop everything still queued and re-raise. In-flight
            # `claude -p` calls still finish or hit their 300s timeout — a Python
            # signal handler cannot interrupt a running subprocess.
            pool = _pool_ref.get("pool")
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
            raise KeyboardInterrupt
        ctx.stopped_by_signal = True
        ctx.stop.set()
        ctx.emit(f"signal {signum} — finishing in-flight stages, then stopping")

    previous_handlers: dict[int, object] = {}
    if handle_signals and threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[sig] = signal.signal(sig, on_signal)
            except (ValueError, OSError):  # pragma: no cover — not the main thread after all
                pass

    log.info("prepare.start", jobs=len(id_list), workers=n_workers, target=ctx.target,
             stages=[s.name for s in stage_specs])

    def record(fut, jid: int) -> None:
        """Book one finished future: summary, running count, report line."""
        try:
            outcome = fut.result()
        except CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — never let one job end the drive
            outcome = JobOutcome(jid, tag=f"[{jid}]", status="?", error=str(exc))
            try:
                final = _peek(jid)
            except Exception:  # noqa: BLE001 — a failed peek must not end the drive either
                final = None
            if final is not None:
                _, _, outcome.status, outcome.score, outcome.reason = final
            else:
                outcome.status = None
        if outcome.skipped:
            return
        summary.outcomes[jid] = outcome
        summary.processed += 1
        if outcome.error:
            summary.errors += 1
        if outcome.covered:
            ctx.covered += 1
            summary.covered = ctx.covered
        ctx.emit(format_outcome(outcome, ctx.covered))

    try:
        with ThreadPoolExecutor(
            max_workers=n_workers, thread_name_prefix="honestapply-prepare"
        ) as pool:
            _pool_ref["pool"] = pool
            futures = {pool.submit(_drive_one, jid, ctx, stage_specs): jid for jid in id_list}
            pending = set(futures)
            for fut in as_completed(futures):
                pending.discard(fut)
                record(fut, futures[fut])
                if ctx.target and ctx.covered >= ctx.target:
                    ctx.target_reached = True
                    ctx.stop.set()
                    ctx.emit(f"target {ctx.target} COVERED reached — stopping")
                    # Drop everything still queued; in-flight jobs see `stop`
                    # and finish after their current stage. NB: cancelled
                    # futures never wake `as_completed` (Future.cancel() skips
                    # its waiters), so leave this loop and drain the running
                    # ones separately rather than waiting here forever.
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
            running = [f for f in pending if not f.cancelled()]
            for fut in as_completed(running):
                record(fut, futures[fut])
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)

    summary.target_reached = ctx.target_reached
    summary.stopped = ctx.stop.is_set() and not ctx.target_reached
    summary.rate_limit_pauses = ctx.pause.triggers - pauses_before
    log.info("prepare.done", covered=summary.covered, processed=summary.processed,
             errors=summary.errors, target_reached=summary.target_reached,
             stopped=summary.stopped, rate_limit_pauses=summary.rate_limit_pauses)
    return summary
