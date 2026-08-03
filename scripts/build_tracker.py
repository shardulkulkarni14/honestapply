"""Regenerate data/application_tracker.md from the DB (+ optional apply_packets.json).

Source of truth = the DB (Job.status + Application rows). `apply_packets.json`
(written when packaging agents resolve real apply URLs) adds apply_url / platform /
login_required. Re-run this after every submission to refresh the board.

    python scripts/build_tracker.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.pop("HONESTAPPLY_DB_PATH", None)
from honestapply.config import PATHS
from honestapply.db.models import Application, Job, Status
from honestapply.db.session import session_scope

READY = {Status.COVERED, Status.DRY_RUN_COMPLETED, Status.APPLIED, Status.NEEDS_HUMAN, Status.FAILED}
PACKETS = PATHS.data_dir / "apply_packets.json"
OUT = PATHS.data_dir / "application_tracker.md"

STATUS_LABEL = {
    Status.APPLIED: "✅ applied",
    Status.DRY_RUN_COMPLETED: "📝 filled (not submitted)",
    Status.COVERED: "🟡 prepared",
    Status.NEEDS_HUMAN: "🙋 needs human",
    Status.FAILED: "❌ failed",
}


def main() -> None:
    packets = {}
    if PACKETS.exists():
        try:
            for rec in json.loads(PACKETS.read_text()):
                packets[int(rec["job_id"])] = rec
        except Exception:
            pass

    rows = []
    with session_scope() as s:
        jobs = [j for j in s.query(Job).all() if j.status in READY and j.tailored_resume_path]
        jobs.sort(key=lambda j: (-(j.score or 0), j.company or ""))
        app_by_job = {}
        for a in s.query(Application).all():
            app_by_job.setdefault(a.job_id, a)
        for j in jobs:
            p = packets.get(j.id, {})
            login = p.get("login_required")
            login_s = {True: "🔒 login", False: "🔓 no-login", "unknown": "❓"}.get(login, "—")
            app = app_by_job.get(j.id)
            submit = STATUS_LABEL.get(j.status, j.status)
            if app and app.mode == "real" and app.status == "applied":
                submit = "✅ submitted"
            elif app and app.mode == "real" and app.status == "rejected":
                submit = "❌ rejected"
            rows.append({
                "score": j.score or "",
                "company": j.company or "",
                "role": (j.title or "")[:42],
                "loc": (j.location or "").split("|")[0].split(",")[0][:14],
                "status": submit,
                "login": login_s,
                "url": p.get("official_apply_url", "_pending_"),
            })

    n = len(rows)
    applied = sum(1 for r in rows if "✅" in r["status"])
    rejected = sum(1 for r in rows if "❌" in r["status"])
    lines = [
        "# honestapply — Application Tracker",
        "",
        f"**{n} prepared · {applied} submitted · {rejected} rejected** · résumé + cover ready for all "
        "(resume / cover-letter PDFs in `data/outputs/<id>/`)",
        "",
        "| Fit | Company | Role | Location | Apply status | Access | Apply link |",
        "|----|---------|------|----------|--------------|--------|------------|",
    ]
    for r in rows:
        url = r["url"]
        url_md = f"[link]({url})" if url and url != "_pending_" else "_pending_"
        lines.append(
            f"| {r['score']} | {r['company']} | {r['role']} | {r['loc']} | "
            f"{r['status']} | {r['login']} | {url_md} |"
        )
    lines += ["", "_Regenerate: `python scripts/build_tracker.py`. Source of truth: the DB._"]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} ({n} applications, {applied} submitted)")


if __name__ == "__main__":
    main()
