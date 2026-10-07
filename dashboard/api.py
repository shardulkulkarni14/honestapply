"""honestapply dashboard — FastAPI backend.

One purpose: a single table with EVERYTHING per application, each cell linking
to the locally-stored artifact (archived JD, tailored resume PDF, cover letter
PDF, submitted form answers, confirmation screenshots, original posting).

Serves the static Next.js export from dashboard/web/out at / when built
(cd dashboard/web && npm install && npm run build), with a no-build fallback
table so the dashboard works even without Node.

Run via `honestapply dashboard` (uvicorn, default port 8501).
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func

from honestapply.config import PATHS
from honestapply.db.events import transition
from honestapply.db.models import Application, Job, JobEvent, Status
from honestapply.db.session import init_db, session_scope

ROOT = PATHS.root
ARCHIVE = ROOT / "data" / "application_archive"
JD_DIR = ARCHIVE / "jd"
WEB_OUT = Path(__file__).resolve().parent / "web" / "out"

app = FastAPI(title="honestapply dashboard", docs_url="/api/docs")
init_db()


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


def _exists(path: str | None) -> bool:
    return bool(path) and Path(path).exists() and Path(path).stat().st_size > 0


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
    entries = _answer_entries()

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
                        "jd": bool(_jd_file(j.id)) or bool(j.description),
                        "resume": _exists(j.tailored_resume_path),
                        "cover": _exists(j.cover_letter_path),
                        "answers": len(_answers_for(j.company or "", entries)),
                        "pre_shot": _exists(latest.pre_submit_screenshot) if latest else False,
                        "post_shot": _exists(latest.post_submit_screenshot) if latest else False,
                    },
                }
            )

    order = {"interviewing": 0, "screening": 1, "applied": 2, "needs_human": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 4), r["applied_at"] == "", r["applied_at"]), reverse=False)
    # within same status, most recent first
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


# Plain-language pipeline stages (the funnel) and outcome buckets, each mapping to
# a set of raw statuses. This is the non-technical "face" of the status taxonomy.
_FUNNEL_STAGES = [
    ("found", "Jobs found", ["discovered"]),
    ("prepared", "Prepared", ["enriched", "scored", "tailored", "covered", "ready_to_apply"]),
    ("applied", "Applied", ["applied", "dry_run_completed"]),
    ("in_process", "In process", ["screening", "interviewing"]),
    ("offer", "Offers", ["offer"]),
]
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
    """The pipeline as plain-language stages + outcome buckets, for the dashboard
    face. Counts are of jobs currently in each status set."""
    by_status: dict[str, int] = {}
    with session_scope() as s:
        for status, n in (
            s.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
        ):
            by_status[status] = n
    total = sum(by_status.values())

    def tally(statuses: list[str]) -> int:
        return sum(by_status.get(st, 0) for st in statuses)

    stages = [{"key": k, "label": lbl, "count": tally(sts)} for k, lbl, sts in _FUNNEL_STAGES]
    outcomes = [
        {"key": k, "label": lbl, "count": tally(sts), "tone": tone}
        for k, lbl, sts, tone in _OUTCOMES
    ]
    return {"total": total, "stages": stages, "outcomes": outcomes, "by_status": by_status}


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
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
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
            self._proc.terminate()


_RUNNER = _Runner()


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
                    "at": ev.at.isoformat() if ev.at else None,
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


@app.patch("/api/jobs/{job_id}")
def patch_job(job_id: int, patch: JobPatch) -> dict:
    if patch.status is not None and patch.status not in Status.ALL:
        raise HTTPException(
            status_code=400,
            detail=f"unknown status {patch.status!r}; must be one of {Status.ALL}",
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
# Frontend: Next.js static export if built, else a built-in fallback table
# ---------------------------------------------------------------------------
_FALLBACK = """<!doctype html><html><head><meta charset="utf-8"><title>honestapply</title>
<style>
 body{background:#0f1115;color:#e7eaf0;font:14px/1.5 -apple-system,Segoe UI,sans-serif;margin:24px}
 a{color:#5b8cff;text-decoration:none} a:hover{text-decoration:underline}
 table{border-collapse:collapse;width:100%} td,th{padding:7px 10px;border-bottom:1px solid #262b36;text-align:left;font-size:13px}
 th{position:sticky;top:0;background:#181b22} input{background:#181b22;color:#e7eaf0;border:1px solid #262b36;border-radius:8px;padding:8px 12px;width:320px;margin:12px 0}
 .b{padding:2px 8px;border-radius:10px;font-size:11px;background:#23364a}
</style></head><body>
<h2>honestapply — applications</h2>
<p style="color:#9aa3b2">Tip: build the full UI with <code>cd dashboard/web && npm install && npm run build</code>, then restart. This fallback works without Node.</p>
<input id="q" placeholder="filter company / role / status…"><div id="t">loading…</div>
<script>
let DATA=[];
const L=(href,txt)=>`<a href="${href}" target="_blank">${txt}</a>`;
function render(){const q=document.getElementById('q').value.toLowerCase();
 const rows=DATA.filter(r=>(r.company+' '+r.title+' '+r.status).toLowerCase().includes(q));
 document.getElementById('t').innerHTML='<table><tr><th>Company</th><th>Role</th><th>Status</th><th>Score</th><th>Applied</th><th>Links</th><th>Next / note</th></tr>'+
 rows.map(r=>{const k=r.links;const ls=[];
  if(r.url)ls.push(L(r.url,'posting'));
  if(k.jd)ls.push(L('/jd/'+r.job_id,'JD'));
  if(k.resume)ls.push(L('/files/'+r.job_id+'/resume','resume'));
  if(k.cover)ls.push(L('/files/'+r.job_id+'/cover','cover'));
  if(k.answers)ls.push(L('/answers/'+r.job_id,'answers('+k.answers+')'));
  if(k.post_shot)ls.push(L('/files/'+r.job_id+'/post_shot','shot'));
  else if(k.pre_shot)ls.push(L('/files/'+r.job_id+'/pre_shot','shot'));
  return `<tr><td><b>${r.company}</b></td><td>${r.title}</td><td><span class="b">${r.status}</span></td><td>${r.score??''}</td><td>${r.applied_at}</td><td>${ls.join(' · ')}</td><td>${r.next_action||r.confirmation.slice(0,80)}</td></tr>`}).join('')+'</table>'}
fetch('/api/applications').then(r=>r.json()).then(d=>{DATA=d;render()});
document.addEventListener('input',render);
</script></body></html>"""


_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='12' fill='#0f1115'/>"
    "<text x='32' y='44' font-family='monospace' font-size='34' fill='#5b8cff' text-anchor='middle'>jp</text></svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response

    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    # Prefer the built Next.js export; otherwise the self-contained editable
    # dashboard (dashboard/index.html) — a real-time page that reads /api and
    # writes back via PATCH, so it never needs a build step. The tiny _FALLBACK
    # remains only for the case where even that file is missing.
    built = WEB_OUT / "index.html"
    if built.exists():
        return HTMLResponse(built.read_text(encoding="utf-8"))
    editable = Path(__file__).parent / "index.html"
    if editable.exists():
        return HTMLResponse(editable.read_text(encoding="utf-8"))
    return HTMLResponse(_FALLBACK)


if WEB_OUT.exists():
    app.mount("/_next", StaticFiles(directory=WEB_OUT / "_next"), name="next-assets")
