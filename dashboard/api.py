"""honestapply dashboard — FastAPI backend.

One purpose: a single table with EVERYTHING per application, each cell linking
to the locally-stored artifact (archived JD, tailored resume PDF, cover letter
PDF, submitted form answers, confirmation screenshots, original posting).

Serves a single self-contained page (dashboard/index.html) at / — no build
step, no Node, no bundler. The page talks to this JSON API directly.

Run via `honestapply dashboard` (uvicorn, default port 8501).
"""

from __future__ import annotations

import atexit
import os
import re
import secrets
import signal
import subprocess
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from honestapply.config import PATHS
from honestapply.db.events import transition
from honestapply.db.models import Application, Job, JobEvent
from honestapply.db.session import init_db, session_scope

ROOT = PATHS.root
ARCHIVE = ROOT / "data" / "application_archive"
JD_DIR = ARCHIVE / "jd"

app = FastAPI(title="honestapply dashboard", docs_url="/api/docs")
init_db()

# --- Local-only auth --------------------------------------------------------
# The dashboard can launch pipeline runs and read/write everything, so even on
# localhost it needs a gate: a malicious web page you visit can POST to
# 127.0.0.1, and a DNS-rebinding attack can masquerade as localhost. A one-time
# token (printed in the terminal, then stored as a SameSite=Strict cookie) plus a
# Host-header check closes both. Set HONESTAPPLY_DASH_TOKEN to pin the token.
DASH_TOKEN = os.environ.get("HONESTAPPLY_DASH_TOKEN") or secrets.token_urlsafe(16)
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "testserver", ""}


@app.middleware("http")
async def _local_auth(request: Request, call_next):
    if request.url.path == "/favicon.ico":
        return await call_next(request)
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in _ALLOWED_HOSTS:  # anti DNS-rebinding
        return Response("Bad host", status_code=400)
    tok = request.cookies.get("ha_token") or request.query_params.get("t") or ""
    if not secrets.compare_digest(tok, DASH_TOKEN):
        return Response(
            "Unauthorized — open the dashboard from the URL printed in your terminal "
            "(it carries a one-time key).",
            status_code=401,
        )
    response = await call_next(request)
    if request.query_params.get("t") and secrets.compare_digest(request.query_params["t"], DASH_TOKEN):
        response.set_cookie("ha_token", DASH_TOKEN, samesite="strict", httponly=True, max_age=86400 * 30)
    return response


# ---------------------------------------------------------------------------
# Answers archive: parse data/application_archive/answers_*.md once per request
# (files are small; no caching keeps it always fresh)
# ---------------------------------------------------------------------------
def _answer_entries() -> list[dict]:
    entries: list[dict] = []
    for f in sorted(ARCHIVE.glob("answers_*.md")):
        text = f.read_text(encoding="utf-8")
        for entry in re.split(r"(?m)^#{2,3} ", text)[1:]:
            title, _, body = entry.partition("\n")
            if title.strip():
                entries.append({"title": title.strip(), "body": body.strip(), "file": f.name})
    return entries


def _company_key(company: str) -> str:
    """First significant token of a company name, for fuzzy matching."""
    cleaned = re.sub(r"\b(gmbh|ag|ug|inc|ltd|labs|technologies)\b", "", (company or "").lower())
    tokens = [t for t in re.split(r"[^a-z0-9]+", cleaned) if len(t) >= 3]
    return tokens[0] if tokens else (company or "").lower().strip()


def _answers_for(company: str, entries: list[dict]) -> list[dict]:
    key = _company_key(company)
    if not key:
        return []
    return [e for e in entries if key in e["title"].lower()]


def _jd_file(job_id: int) -> Path | None:
    hits = sorted(JD_DIR.glob(f"{job_id}_*.md")) if JD_DIR.exists() else []
    return hits[0] if hits else None


# Perf: /api/applications used to glob the JD dir and re-parse every answers file
# once PER ROW (×~1000) on every 15s poll. Build the JD-id set with one iterdir,
# and cache the parsed answers until the files change.
def _jd_job_ids() -> set[int]:
    ids: set[int] = set()
    if JD_DIR.exists():
        for p in JD_DIR.iterdir():
            head = p.name.split("_", 1)[0]
            if p.suffix == ".md" and head.isdigit():
                ids.add(int(head))
    return ids


_answers_cache: dict = {"sig": None, "entries": []}


def _answer_entries_cached() -> list[dict]:
    files = sorted(ARCHIVE.glob("answers_*.md")) if ARCHIVE.exists() else []
    sig = tuple((f.name, f.stat().st_mtime) for f in files)
    if _answers_cache["sig"] != sig:
        _answers_cache["sig"] = sig
        _answers_cache["entries"] = _answer_entries()
    return _answers_cache["entries"]


def _exists(path: str | None) -> bool:
    return bool(path) and Path(path).exists() and Path(path).stat().st_size > 0


def _iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 WITH a UTC offset. Event timestamps are stored UTC (utcnow) but
    SQLite hands them back naive; without the offset the browser parses them as
    local time and "2h ago" is wrong by the user's UTC offset."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Matches "Wed 9 Sep 2026 13:30" / "Mon 7 Sep 2026 10" (time optional) as
# written into a job's status_reason/notes when an interview is scheduled.
_INTERVIEW_DATE_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})"
    r"(?:\s+(\d{1,2})(?::(\d{2}))?)?"
)


def _next_interview(*texts: str) -> tuple[str, str]:
    """Extract the first scheduled interview date from the given free-text
    fields. Returns (iso_for_sorting, human_display), or ("", "") if none."""
    for text in texts:
        if not text:
            continue
        m = _INTERVIEW_DATE_RE.search(text)
        if not m:
            continue
        day, year = int(m.group(1)), int(m.group(3))
        month = _MONTHS[m.group(2)]
        hour = int(m.group(4)) if m.group(4) else 0
        minute = int(m.group(5)) if m.group(5) else 0
        try:
            dt = datetime(year, month, day, hour, minute)
        except ValueError:
            continue
        disp = f"{_WEEKDAYS[dt.weekday()]} {day} {m.group(2)}"
        if m.group(4):
            disp += f" {hour:02d}:{minute:02d}"
        return dt.strftime("%Y-%m-%dT%H:%M"), disp
    return "", ""


@app.get("/api/applications")
def applications() -> list[dict]:
    """One row per application — everything the table needs, links included."""
    entries = _answer_entries_cached()
    jd_ids = _jd_job_ids()

    rows: list[dict] = []
    with session_scope() as s:
        from sqlalchemy import or_, select
        from sqlalchemy.orm import joinedload

        # Show a job if it has an application row OR its status is one we actively
        # track. The second case matters for inbound-recruiter interviews (e.g. a
        # recruiter reaches out, we go straight to interviewing) that never went
        # through the apply stage and so have no Application row — an inner join
        # on Application would silently drop them.
        tracked = {
            "applied", "needs_human", "failed",
            "screening", "interviewing", "offer", "rejected", "ghosted",
        }
        jobs = (
            s.execute(
                select(Job)
                .outerjoin(Application, Application.job_id == Job.id)
                .where(or_(Application.id.isnot(None), Job.status.in_(tracked)))
                .options(joinedload(Job.applications))
                .distinct()
            )
            .unique()
            .scalars()
            .all()
        )
        for j in jobs:
            apps = sorted(j.applications, key=lambda a: a.applied_at or 0, reverse=True)
            latest = apps[0] if apps else None
            # The job's own status is authoritative now that post-apply outcomes
            # (screening/interviewing/offer/rejected/ghosted) are real statuses,
            # set on the job itself. This replaces the old company-name match
            # against active_pipeline.json, which collided across employers
            # sharing a first token (Deutsche Bank / Deutsche Telekom).
            status = j.status or (latest.status if latest else "")
            ni_iso, ni_disp = _next_interview(j.status_reason or "", j.notes or "")
            rows.append(
                {
                    "job_id": j.id,
                    "company": j.company or "",
                    "title": j.title or "",
                    "location": (j.location or "").split("|")[0].strip(),
                    "status": status or "",
                    "score": j.score,
                    "applied_at": latest.applied_at.strftime("%Y-%m-%d") if latest and latest.applied_at else "",
                    "url": j.url or "",
                    "notes": j.notes or "",
                    "status_reason": j.status_reason or "",
                    "next_interview": ni_disp,
                    "next_interview_iso": ni_iso,
                    "confirmation": (latest.confirmation_text or "")[:300] if latest else "",
                    "links": {
                        "jd": (j.id in jd_ids) or bool(j.description),
                        "resume": _exists(j.tailored_resume_path),
                        "cover": _exists(j.cover_letter_path),
                        "answers": len(_answers_for(j.company or "", entries)),
                        "pre_shot": _exists(latest.pre_submit_screenshot) if latest else False,
                        "post_shot": _exists(latest.post_submit_screenshot) if latest else False,
                    },
                }
            )

    # Status priority first, most-recent within each (stable sorts, applied last).
    order = {"interviewing": 0, "screening": 1, "applied": 2, "needs_human": 3}
    rows.sort(key=lambda r: r["applied_at"], reverse=True)
    rows.sort(key=lambda r: order.get(r["status"], 4))
    return rows


@app.get("/api/summary")
def summary() -> dict:
    rows = applications()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"total": len(rows), "by_status": counts}


# Cumulative funnel stages: "ever reached this stage or beyond". Each maps to the
# set of statuses that imply the stage was reached. Counted from each job's CURRENT
# status (its own authoritative state), NOT from job_events — because a job's
# status is complete for every job, whereas event history is missing for jobs that
# were set directly (imports/older rows). Because the status taxonomy is a
# progression, a downstream status still implies every upstream stage, so the
# counts are monotonically non-increasing (a real funnel). "Jobs found" is the KPI
# total, not a bar.
_FUNNEL_STAGES = [
    ("prepared", "Prepared",
     {"covered", "ready_to_apply", "needs_human", "applied", "dry_run_completed",
      "screening", "interviewing", "offer", "rejected", "ghosted"}),
    ("applied", "Applied",
     {"applied", "dry_run_completed", "screening", "interviewing", "offer", "rejected", "ghosted"}),
    ("in_process", "In process", {"screening", "interviewing", "offer"}),
    ("offer", "Offers", {"offer"}),
]
# Outcome buckets — these ARE current-state (where the job stands now).
_OUTCOMES = [
    ("offer", "Offers", ["offer"], "good"),
    ("in_process", "In process", ["screening", "interviewing"], "info"),
    ("needs_human", "Needs you", ["needs_human"], "warning"),
    ("rejected", "Not selected", ["rejected"], "critical"),
    ("ghosted", "No response", ["ghosted"], "muted"),
    ("skipped", "Skipped", ["skipped_low_fit", "skipped_company_cap", "failed"], "muted"),
]


@app.get("/api/funnel")
def funnel() -> dict:
    """The pipeline as a cumulative funnel (ever-reached, from job_events) plus
    current-state outcome buckets, for the dashboard face."""
    with session_scope() as s:
        total = s.query(func.count(Job.id)).scalar() or 0
        by_status = dict(s.query(Job.status, func.count(Job.id)).group_by(Job.status).all())

        # A job counts toward a stage if its CURRENT status implies it (this covers
        # every job, including the ~⅔ that have no event rows) OR any event shows it
        # reached there (adds the real path for jobs that have since moved on — e.g.
        # a now-rejected job that passed through screening still counts "In process").
        def reached(statuses: set[str]) -> int:
            cur = set(s.execute(select(Job.id).where(Job.status.in_(statuses))).scalars())
            evt = set(s.execute(select(JobEvent.job_id).where(JobEvent.to_status.in_(statuses))).scalars())
            return len(cur | evt)

        stages = [{"key": k, "label": lbl, "count": reached(sts)} for k, lbl, sts in _FUNNEL_STAGES]

    outcomes = [
        {"key": k, "label": lbl, "count": sum(by_status.get(st, 0) for st in sts), "tone": tone}
        for k, lbl, sts, tone in _OUTCOMES
    ]
    return {"total": total, "scanned": total, "stages": stages, "outcomes": outcomes, "by_status": by_status}


# ---------------------------------------------------------------------------
# Run control (Phase 2): start a pipeline stage as a background subprocess and
# watch progress. Single-run only (apply is never parallel). Real submissions are
# NOT triggerable here — the UI offers discover / prepare / apply-DRY-RUN only;
# real applies stay on the CLI behind their confirm-first rule.
# ---------------------------------------------------------------------------
_RUN_STAGES = {
    "discover": ["honestapply", "discover"],
    "prepare": ["honestapply", "run"],
    "apply_dry": ["honestapply", "apply", "--dry-run"],
}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _Runner:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._stage: str | None = None
        self._started: str | None = None
        self._rc: int | None = None
        self._lines: deque[str] = deque(maxlen=60)
        self._lock = threading.Lock()

    @property
    def _running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        return {
            "running": self._running,
            "stage": self._stage,
            "started_at": self._started,
            "returncode": None if self._running else self._rc,
            "log": list(self._lines)[-10:],
        }

    def start(self, stage: str) -> None:
        if stage not in _RUN_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        with self._lock:
            if self._running:
                raise RuntimeError("a run is already in progress")
            self._lines.clear()
            self._stage, self._rc = stage, None
            self._started = datetime.now(timezone.utc).isoformat()
            self._proc = subprocess.Popen(  # noqa: S603 — fixed command set
                _RUN_STAGES[stage],
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,  # a stage that ever prompts gets EOF, not a hang
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,  # own process group, so we can kill children (Chromium)
            )
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = _ANSI_RE.sub("", raw).rstrip()
            if line:
                self._lines.append(line[:300])
        self._rc = self._proc.wait()

    def stop(self) -> None:
        if self._running and self._proc is not None:
            # Kill the whole process group so browser/child processes die too.
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                self._proc.terminate()


_RUNNER = _Runner()
# Don't leave a run (and its browser children) alive after the server exits.
atexit.register(_RUNNER.stop)


class RunReq(BaseModel):
    stage: str


@app.get("/api/run")
def run_status() -> dict:
    return _RUNNER.status()


@app.post("/api/run")
def run_start(req: RunReq) -> dict:
    try:
        _RUNNER.start(req.stage)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _RUNNER.status()


@app.post("/api/run/stop")
def run_stop() -> dict:
    _RUNNER.stop()
    return _RUNNER.status()


@app.get("/api/activity")
def activity(limit: int = 40) -> list[dict]:
    """Recent status changes across all jobs, newest first — the live feed."""
    limit = max(1, min(limit, 200))
    out: list[dict] = []
    with session_scope() as s:
        q = (
            s.query(JobEvent, Job)
            .join(Job, Job.id == JobEvent.job_id)
            .order_by(JobEvent.at.desc())
            .limit(limit)
        )
        for ev, job in q.all():
            out.append(
                {
                    "job_id": job.id,
                    "company": job.company,
                    "title": job.title,
                    "from": ev.from_status,
                    "to": ev.to_status,
                    "source": ev.source,
                    "note": ev.note,
                    "at": _iso_utc(ev.at),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Writes: the dashboard can now change a job's status and note. Every status
# change is recorded in job_events, attributed to "dashboard", by the same
# before_flush listener the pipeline uses — the API doesn't write events itself.
# ---------------------------------------------------------------------------
class JobPatch(BaseModel):
    status: str | None = None
    notes: str | None = None  # running note on the job (jobs.notes)
    event_note: str | None = None  # note attached to *this* status transition


# Statuses a human may set by hand from the dashboard — outcomes only. Setting a
# pipeline-internal status (e.g. ready_to_apply, discovered) from the UI would
# re-arm the automation on that job, which a non-technical user would do by
# accident; the dropdown already hides these, and this is the backend guard.
_DASHBOARD_SETTABLE = frozenset(
    {"applied", "screening", "interviewing", "offer", "rejected", "ghosted", "needs_human"}
)


@app.patch("/api/jobs/{job_id}")
def patch_job(job_id: int, patch: JobPatch) -> dict:
    if patch.status is not None and patch.status not in _DASHBOARD_SETTABLE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"status {patch.status!r} can't be set from the dashboard; "
                f"choose one of {sorted(_DASHBOARD_SETTABLE)}"
            ),
        )
    # The context manager must enclose the commit (where the flush fires), so the
    # listener sees this change as dashboard-sourced with the given note.
    with transition("dashboard", note=patch.event_note):
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no job {job_id}")
            if patch.status is not None:
                job.status = patch.status
            if patch.notes is not None:
                job.notes = patch.notes
            result = {"job_id": job.id, "status": job.status, "notes": job.notes or ""}
    return result


@app.get("/api/analytics")
def analytics() -> dict:
    """Outcome analytics — response/interview rates by ATS, role, and score band,
    time-to-response, and a data-driven min-score recommendation."""
    from honestapply.analytics import compute

    with session_scope() as s:
        return compute(s)


@app.get("/api/jobs/{job_id}/provenance")
def provenance(job_id: int) -> dict:
    """Attestation that every claim in the tailored résumé traces to source and
    survives into the parsed PDF — the no-fabrication guarantee, made inspectable."""
    from honestapply.config import PATHS as _PATHS
    from honestapply.provenance import attest_job

    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        att = attest_job(job, _PATHS.resumes_dir)
        if att is None:
            raise HTTPException(status_code=409, detail="job is not tailored yet")
        return att.as_dict()


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: int) -> list[dict]:
    """The timeline behind a row: every status transition (from job_events) plus
    every submission attempt (from applications), interleaved by time. Status
    changes are the phases the job moved through; application rows show when a
    real or dry-run submission actually happened."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")

        items: list[dict] = []
        for e in job.events:
            items.append(
                {
                    "kind": "status",
                    "id": e.id,
                    "from": e.from_status,
                    "to": e.to_status,
                    "at": e.at.isoformat() if e.at else None,
                    "source": e.source,
                    "note": e.note,
                }
            )
        for a in job.applications:
            # Which screenshot this specific attempt actually has (a failed/dry
            # attempt may have captured only the pre-submit one). None => no link.
            shot_kind = (
                "post_shot" if a.post_submit_screenshot
                else "pre_shot" if a.pre_submit_screenshot
                else None
            )
            items.append(
                {
                    "kind": "application",
                    "id": a.id,
                    "at": a.applied_at.isoformat() if a.applied_at else None,
                    "mode": a.mode,
                    "status": a.status,
                    "note": (a.confirmation_text or "")[:200] or None,
                    "shot_kind": shot_kind,
                    "has_screenshot": shot_kind is not None,
                }
            )
        # Sort by timestamp; ties break status-before-application then by id so
        # the order is stable and deterministic. Items without a timestamp (rare)
        # fall to the end.
        _kind_rank = {"status": 0, "application": 1}
        items.sort(
            key=lambda it: (
                it["at"] is None,
                it["at"] or "",
                _kind_rank.get(it["kind"], 9),
                it["id"],
            )
        )
        return items


class EventNote(BaseModel):
    note: str  # a note to append to the history at the current status


@app.post("/api/jobs/{job_id}/events")
def add_event_note(job_id: int, body: EventNote) -> dict:
    """Append a dated note to a job's history at its current status.

    The history is append-only: this adds a new entry, it never edits or deletes
    an existing one. It records a same-status marker (from == to == current
    status), attributed to the dashboard, so notes like an interview-round detail
    live in the timeline instead of overwriting anything."""
    note = (body.note or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="note must not be empty")
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        current = job.status or ""
        ev = JobEvent(
            job_id=job.id,
            from_status=current or None,
            to_status=current,
            source="dashboard",
            note=note,
        )
        s.add(ev)
        s.flush()
        return {
            "id": ev.id,
            "job_id": job.id,
            "to": ev.to_status,
            "at": ev.at.isoformat() if ev.at else None,
            "note": ev.note,
        }


# ---------------------------------------------------------------------------
# Local artifacts
# ---------------------------------------------------------------------------
@app.get("/jd/{job_id}", response_class=PlainTextResponse)
def jd(job_id: int) -> str:
    f = _jd_file(job_id)
    if f:
        return f.read_text(encoding="utf-8")
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j and j.description:
            return f"# {j.title} @ {j.company}\n\n{j.description}"
    raise HTTPException(404, "no archived JD for this job")


@app.get("/answers/{job_id}", response_class=PlainTextResponse)
def answers(job_id: int) -> str:
    with session_scope() as s:
        j = s.get(Job, job_id)
        if not j:
            raise HTTPException(404, "job not found")
        company = j.company or ""
    matched = _answers_for(company, _answer_entries())
    if not matched:
        raise HTTPException(404, f"no archived answers matched company {company!r}")
    parts = [f"=== {e['title']}  ({e['file']}) ===\n\n{e['body']}" for e in matched]
    return "\n\n\n".join(parts)


_FILE_KINDS = {
    "resume": ("tailored_resume_path", "application/pdf"),
    "cover": ("cover_letter_path", "application/pdf"),
}


@app.get("/files/{job_id}/{kind}")
def files(job_id: int, kind: str) -> FileResponse:
    with session_scope() as s:
        j = s.get(Job, job_id)
        if not j:
            raise HTTPException(404, "job not found")
        if kind in _FILE_KINDS:
            attr, media = _FILE_KINDS[kind]
            path = getattr(j, attr)
        elif kind in ("pre_shot", "post_shot"):
            apps = sorted(j.applications, key=lambda a: a.applied_at or 0, reverse=True)
            if not apps:
                raise HTTPException(404, "no application")
            path = apps[0].pre_submit_screenshot if kind == "pre_shot" else apps[0].post_submit_screenshot
            media = "image/png"
        else:
            raise HTTPException(404, "unknown kind")
    if not _exists(path):
        raise HTTPException(404, f"{kind} file missing")
    return FileResponse(path, media_type=media, content_disposition_type="inline")


@app.get("/files/app/{application_id}/{kind}")
def application_file(application_id: int, kind: str) -> FileResponse:
    """A screenshot for one specific submission attempt, addressed by its own id.

    The timeline links here (not to ``/files/{job_id}/..``, which always serves
    the newest attempt) so that in a multi-application job each row opens its own
    pre/post screenshot rather than the latest one."""
    if kind not in ("pre_shot", "post_shot"):
        raise HTTPException(404, "unknown kind")
    with session_scope() as s:
        a = s.get(Application, application_id)
        if not a:
            raise HTTPException(404, "application not found")
        path = a.pre_submit_screenshot if kind == "pre_shot" else a.post_submit_screenshot
    if not _exists(path):
        raise HTTPException(404, f"{kind} file missing")
    return FileResponse(path, media_type="image/png", content_disposition_type="inline")


# ---------------------------------------------------------------------------
# Frontend: the self-contained editable dashboard (dashboard/index.html). It's a
# real-time page that reads /api and writes back via PATCH, so it needs no build
# step — which is exactly what a non-technical user self-hosting it wants.
# ---------------------------------------------------------------------------
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='12' fill='#0f1115'/>"
    "<text x='32' y='45' font-family='-apple-system,sans-serif' font-weight='700' "
    "font-size='30' fill='#818cf8' text-anchor='middle'>ha</text></svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response

    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    page = Path(__file__).parent / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="dashboard/index.html is missing")
    return HTMLResponse(page.read_text(encoding="utf-8"))
