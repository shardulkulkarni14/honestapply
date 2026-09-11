"""Shared plumbing for running prepare stages on a bounded thread pool.

The prepare stages (enrich, score, tailor, cover-letter) are bound by the LLM
round-trip, not the CPU: `claude -p` spawns a process and takes 10-60s per
call, during which the GIL is released. A small pool of threads overlaps those
waits. Three rules make that safe:

* **The DB is the queue.** There is no in-memory work list to keep consistent;
  a job's ``status`` column says which stage it is waiting for.
* **One short transaction per stage per job.** A worker reads the job, does the
  slow work with no lock held, then commits through a compare-and-set
  (:func:`honestapply.db.session.commit_if_status`) that only lands if the job
  is still where this stage found it. Two workers — or two processes — can
  never both advance the same job.
* **Failures are isolated.** An exception in one job never touches another
  job's transaction or stops the pool.

Nothing here is used by the apply stage, which stays a single serialised
consumer on purpose (see scripts/apply_consumer.sh).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, TypeVar

from honestapply.config import get_settings
from honestapply.db.models import Job, Status
from honestapply.db.session import commit_if_status, session_scope
from honestapply.llm.base import LLMError
from honestapply.logging_setup import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# What an LLM provider says when the problem is *us calling too often*, not the
# job. These are transient: the job is left where it is and retried later,
# rather than being marked failed like a genuine per-job error.
_RATE_LIMIT_RE = re.compile(r"usage limit|rate limit|rate_limit|\b429\b|overloaded", re.IGNORECASE)


def is_rate_limit(exc: BaseException) -> bool:
    return isinstance(exc, LLMError) and bool(_RATE_LIMIT_RE.search(str(exc)))


def resolve_workers(workers: int | None) -> int:
    """The worker count to use: the explicit value, else the settings default;
    never below 1. Also widens the process-wide `claude -p` gate to match."""
    n = workers if workers is not None and workers > 0 else get_settings().honestapply_prepare_workers
    n = max(1, int(n))
    from honestapply.llm import claude_cli_provider

    claude_cli_provider.set_max_concurrent(n)
    return n


class LLMPause:
    """A pause shared by every worker in a process.

    When one worker is told to back off (usage limit, 429, overloaded), it is
    pointless for the others to keep firing calls that will fail the same way
    and burn the remaining candidates. :meth:`trigger` sets a deadline; every
    worker calls :meth:`wait` before its next LLM stage and sleeps until then.
    """

    def __init__(self, seconds: float | None = None, give_up_after: int | None = None) -> None:
        self._seconds = seconds
        self._give_up_after = give_up_after
        self._until = 0.0
        self._lock = threading.Lock()
        self.triggers = 0
        self.consecutive = 0  # consecutive triggers with no success in between

    @property
    def seconds(self) -> float:
        if self._seconds is not None:
            return self._seconds
        return float(get_settings().honestapply_llm_pause_seconds)

    @property
    def give_up_after(self) -> int:
        if self._give_up_after is not None:
            return self._give_up_after
        return int(get_settings().honestapply_llm_pause_give_up_after)

    def should_give_up(self) -> bool:
        """True once the limit has persisted across `give_up_after` consecutive
        pauses with no successful call — the run should stop rather than sleep
        through every remaining job."""
        n = self.give_up_after
        return n > 0 and self.consecutive >= n

    def note_progress(self) -> None:
        """A call succeeded → the limit has cleared; reset the give-up streak."""
        with self._lock:
            self.consecutive = 0

    def trigger(self, reason: str = "") -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + self.seconds)
            self.triggers += 1
            self.consecutive += 1
        logger.warning(
            "llm rate limit hit — pausing all workers for %ss: %s", self.seconds, reason[:200]
        )

    def remaining(self) -> float:
        return max(0.0, self._until - time.monotonic())

    def wait(self, stop_event: threading.Event | None = None) -> float:
        """Block until the pause has passed (or *stop_event* is set). Returns
        the seconds actually waited."""
        waited = 0.0
        while (left := self.remaining()) > 0 and not (stop_event and stop_event.is_set()):
            step = min(left, 1.0)
            time.sleep(step)
            waited += step
        return waited


# One pause per process, shared by the stage runners and the prepare pool.
PAUSE = LLMPause()


def run_stage_on_job(
    jid: int,
    *,
    stage: str,
    expects: str,
    work: Callable[[Job], None],
    mark_failed: bool = True,
) -> str | None:
    """Run one stage on one job in its own transaction.

    Reads the job, skips it unless it is in *expects*, runs *work* (which
    mutates the ORM object; the LLM call lives in there, with no lock held),
    then commits through the compare-and-set. Returns the job's status after
    the commit, or None if the stage did not apply (wrong status on read, or
    another worker got there first).

    Errors: with *mark_failed* the job is set to ``failed`` with the reason —
    what the stage runners have always done. Without it the exception
    propagates and the transaction rolls back, leaving the job untouched for a
    later run — what the batch driver has always done. A rate-limit error is
    never marked failed either way: it says nothing about the job.
    """
    with session_scope() as session:
        job = session.get(Job, jid)
        if job is None or job.status != expects:
            return None
        try:
            work(job)
        except Exception as exc:
            if not mark_failed or is_rate_limit(exc):
                raise
            if isinstance(exc, LLMError):
                logger.error("%s: job %d LLM error: %s", stage, jid, exc)
            else:
                logger.exception("%s: job %d unexpected error: %s", stage, jid, exc)
            job.status = Status.FAILED
            job.status_reason = str(exc)
        if not commit_if_status(session, job, expects):
            logger.info("%s: job %d left %s while we worked — result discarded", stage, jid, expects)
            return None
        return job.status


def run_pool(
    fn: Callable[[int], T],
    ids: Iterable[int],
    workers: int | None,
    *,
    label: str = "stage",
    pause: LLMPause | None = None,
    stop_event: threading.Event | None = None,
) -> list[T | None]:
    """Apply *fn* to every id on a pool of *workers* threads (1 = in-line).

    Each call is guarded: an exception is logged and yields None for that id,
    and a rate-limit error also triggers the shared *pause* so the remaining
    calls wait it out. Results are in completion order.
    """
    n = resolve_workers(workers)
    pause = pause or PAUSE
    id_list = list(dict.fromkeys(ids))
    gave_up = threading.Event()

    def guarded(jid: int) -> T | None:
        if gave_up.is_set() or (stop_event is not None and stop_event.is_set()):
            return None
        if pause.should_give_up():
            gave_up.set()
            if stop_event is not None:
                stop_event.set()
            logger.warning(
                "%s: giving up — usage limit persisted across %d pauses", label, pause.consecutive
            )
            return None
        pause.wait(stop_event)
        try:
            result = fn(jid)
            pause.note_progress()
            return result
        except Exception as exc:  # noqa: BLE001 — one job must never take down the pool
            if is_rate_limit(exc):
                pause.trigger(str(exc))
            logger.warning("%s: job %d error: %s", label, jid, exc)
            return None

    if n <= 1 or len(id_list) <= 1:
        return [guarded(jid) for jid in id_list]

    results: list[Any] = []
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"honestapply-{label}") as pool:
        futures = [pool.submit(guarded, jid) for jid in id_list]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results
