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
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from honestapply.config import PATHS
from honestapply.db.events import transition
from honestapply.db.models import Application, Job, Status
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
@app.get("/api/applications")
def applications() -> list[dict]:
    """One row per application — everything the table needs, links included."""
    entries = _answer_entries()

    rows: list[dict] = []
    with session_scope() as s:
        from sqlalchemy import select
        from sqlalchemy.orm import joinedload

        jobs = (
            s.execute(
                select(Job)
                .join(Application, Application.job_id == Job.id)
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


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: int) -> list[dict]:
    """The status history of one job — the timeline behind a row."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        return [
            {
                "from": e.from_status,
                "to": e.to_status,
                "at": e.at.isoformat() if e.at else None,
                "source": e.source,
                "note": e.note,
            }
            for e in sorted(job.events, key=lambda e: e.at or e.id)
        ]


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
    built = WEB_OUT / "index.html"
    if built.exists():
        return HTMLResponse(built.read_text(encoding="utf-8"))
    return HTMLResponse(_FALLBACK)


if WEB_OUT.exists():
    app.mount("/_next", StaticFiles(directory=WEB_OUT / "_next"), name="next-assets")
