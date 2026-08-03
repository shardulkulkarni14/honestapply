"""Build ONE consolidated job-search dashboard (data/dashboard.html).

Merges every application from BOTH tracking systems into a single place:
  1. honestapply DB            -> data/honestapply.db          (this project, autonomous pipeline)
  2. cowork Excel tracker   -> an optional external Excel tracker ($HONESTAPPLY_COWORK_XLSX)

Also surfaces the rejected/skipped list, interview prep, and submitted screening
answers from the cowork sheet so it really is "one place for everything".

Run:  python scripts/build_dashboard.py
The Excel is read with stdlib only (zipfile+regex) so no openpyxl dependency.
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "honestapply.db"
ACTIVE_JSON = ROOT / "data" / "active_pipeline.json"
# Optional external Excel tracker to merge in (set the env var to your file;
# silently skipped when unset or missing).
COWORK_XLSX = Path(os.environ.get("HONESTAPPLY_COWORK_XLSX") or ROOT / "data" / "external_tracker.xlsx")
# Display name used in the dashboard title.
OWNER = os.environ.get("HONESTAPPLY_OWNER_NAME") or "honestapply"
OUT = ROOT / "data" / "dashboard.html"

GENERATED = "2026-06-09"  # stamp (Date.now is unavailable in the harness env)

# ---- status normalisation -------------------------------------------------
# class -> used for colour + filtering; label -> shown in the cell
def norm_status(raw: str) -> tuple[str, str]:
    s = (raw or "").strip().lower()
    if s in {"interviewing", "interview"}:
        return "interviewing", "Interviewing"
    if s in {"applied"}:
        return "applied", "Applied"
    if s in {"rejected"}:
        return "rejected", "Rejected"
    if s in {"dry_run_completed", "dry_run", "needs_human", "filled"}:
        return "filled", "Filled · not submitted"
    if s in {"screening", "in process"}:
        return "screening", "Screening"
    return "other", raw or "—"


# ---- read the cowork .xlsx with stdlib only -------------------------------
def read_xlsx(path: Path) -> dict[str, list[list[str]]]:
    if not path.exists():
        return {}
    z = zipfile.ZipFile(path)
    names = re.findall(r'<sheet[^>]*name="([^"]+)"', z.read("xl/workbook.xml").decode("utf-8", "ignore"))
    sheet_files = sorted(
        (n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)", n).group(1)),
    )

    def cell(c: str) -> str:
        m = re.search(r"<t[^>]*>(.*?)</t>", c, re.S)
        if m:
            return html.unescape(m.group(1))
        v = re.search(r"<v>(.*?)</v>", c, re.S)
        return html.unescape(v.group(1)) if v else ""

    out: dict[str, list[list[str]]] = {}
    for name, nf in zip(names, sheet_files):
        xml = z.read(nf).decode("utf-8", "ignore")
        rows = []
        for r in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
            rows.append([cell(c) for c in re.findall(r"<c[^>]*?>(.*?)</c>", r, re.S)])
        out[name] = rows
    return out


# ---- gather all application records ---------------------------------------
def gather() -> tuple[list[dict], dict, list[dict]]:
    records: list[dict] = []

    # 0) ACTIVE pipeline (manually tracked: interviews, recruiter screens, screenings)
    active: list[dict] = []
    if ACTIVE_JSON.exists():
        active = json.loads(ACTIVE_JSON.read_text())
    for a in active:
        cls, label = norm_status(a.get("status", ""))
        note = a.get("stage", "")
        if a.get("via"):
            note += f" · via {a['via']}"
        if a.get("next_action"):
            note += f" · {a['next_action']}"
        records.append({
            "company": a["company"], "role": a["role"], "location": a.get("location", ""),
            "source": "active", "status_cls": cls, "status": label,
            "applied": "", "salary": a.get("salary", ""), "link": a.get("link", ""),
            "notes": note, "jd": "",
        })
    active_companies = {a["company"] for a in active}

    # 1) cowork Excel "Job Applications"
    sheets = read_xlsx(COWORK_XLSX)
    for row in sheets.get("Job Applications", [])[1:]:
        row = (row + [""] * 11)[:11]
        company, role, location, _type, status, applied, salary, _res, _cov, selling, notes = row
        if not company:
            continue
        # skip rows now superseded by the ACTIVE pipeline (avoid duplicates)
        if company in active_companies:
            continue
        cls, label = norm_status(status)
        records.append({
            "company": company, "role": role, "location": location,
            "source": "cowork", "status_cls": cls, "status": label,
            "applied": applied or "", "salary": salary or "",
            "link": "", "notes": (notes or "") + ((" · " + selling) if selling else ""),
            "jd": "",
        })

    # 2) honestapply DB
    if DB.exists():
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        q = """
            SELECT j.company, j.title, j.location, j.url, j.ats_type, j.score,
                   COALESCE(j.description,'') jd, a.applied_at, a.status,
                   COALESCE(a.confirmation_text,'') conf
            FROM applications a JOIN jobs j ON j.id = a.job_id
            ORDER BY a.applied_at DESC
        """
        for r in con.execute(q):
            cls, label = norm_status(r["status"])
            records.append({
                "company": r["company"], "role": r["title"],
                "location": (r["location"] or "").split("|")[0],
                "source": "honestapply", "status_cls": cls, "status": label,
                "applied": (r["applied_at"] or "")[:10], "salary": "",
                "link": r["url"] or "", "notes": r["conf"] or "", "jd": r["jd"],
            })
        con.close()

    # summary counts
    summary = {
        "total": len(records),
        "interviewing": sum(r["status_cls"] == "interviewing" for r in records),
        "applied": sum(r["status_cls"] == "applied" for r in records),
        "screening": sum(r["status_cls"] == "screening" for r in records),
        "rejected": sum(r["status_cls"] == "rejected" for r in records),
        "filled": sum(r["status_cls"] == "filled" for r in records),
        "companies": len({r["company"] for r in records}),
    }
    return records, summary, active


ARCHIVE_DIR = ROOT / "data" / "application_archive"


def archive_answers_html() -> str:
    """Every answer actually typed into a real form, from the local archive
    (data/application_archive/answers_*.md) — one collapsible block per application."""
    blocks: list[str] = []
    for f in sorted(ARCHIVE_DIR.glob("answers_*.md")):
        text = f.read_text(encoding="utf-8")
        for entry in re.split(r"(?m)^#{2,3} ", text)[1:]:
            title, _, body = entry.partition("\n")
            if not title.strip():
                continue
            blocks.append(
                f"<details class='qa'><summary><b>{esc(title.strip())}</b> "
                f"<span class='muted'>· {esc(f.name)}</span></summary>"
                f"<pre style='white-space:pre-wrap;font-size:12px;color:#c6cbd4'>{esc(body.strip())}</pre></details>"
            )
    return "".join(blocks)


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def main() -> None:
    records, summary, active = gather()
    sheets = read_xlsx(COWORK_XLSX)
    skipped = sheets.get("Rejected - Skipped", [])[1:]
    prep = sheets.get("Interview Prep", [])[1:]
    answers = sheets.get("Application Answers", [])[1:]

    # sort: interviewing first, then screening, applied, filled, rejected; recent on top
    order = {"interviewing": 0, "screening": 1, "applied": 2, "filled": 3, "other": 4, "rejected": 5}
    records.sort(key=lambda r: (order.get(r["status_cls"], 9), r["applied"] == "", -(_ord(r["applied"]))))

    # active-pipeline cards (next actions)
    active_cards = []
    for a in active:
        cls, _ = norm_status(a.get("status", ""))
        meta = " · ".join(x for x in [a.get("location"), a.get("salary"), a.get("via") and ("via " + a["via"])] if x)
        link = f'<a href="{esc(a.get("link",""))}" target="_blank" rel="noopener">open ↗</a>' if a.get("link") else ""
        active_cards.append(
            f'<div class="act act-{cls}"><div class="act-h"><b>{esc(a["company"])}</b> — {esc(a["role"])} '
            f'<span class="pill {cls}">{esc(a.get("stage",""))}</span></div>'
            f'<div class="act-m">{esc(meta)} {link}</div>'
            f'<div class="act-n">{esc(a.get("next_action",""))}</div></div>'
        )

    rows_html = []
    for r in records:
        role_cell = esc(r["role"])
        if r["link"]:
            role_cell = f'<a href="{esc(r["link"])}" target="_blank" rel="noopener">{esc(r["role"])}</a>'
        jd_cell = "—"
        if r.get("jd"):
            jd_cell = f'<details><summary>📄&nbsp;JD</summary><pre>{esc(r["jd"])}</pre></details>'
        rows_html.append(
            f'<tr data-status="{r["status_cls"]}" data-source="{r["source"]}" '
            f'data-text="{esc((r["company"]+" "+r["role"]+" "+r["location"]).lower())}">'
            f'<td class="co">{esc(r["company"])}</td>'
            f'<td>{role_cell}</td>'
            f'<td class="loc">{esc(r["location"])}</td>'
            f'<td><span class="src src-{r["source"]}">{r["source"]}</span></td>'
            f'<td><span class="pill {r["status_cls"]}">{esc(r["status"])}</span></td>'
            f'<td class="dt">{esc(r["applied"])}</td>'
            f'<td class="jd">{jd_cell}</td>'
            f'<td class="nt">{esc(r["notes"][:200])}</td>'
            f'</tr>'
        )

    skipped_html = "".join(
        f"<tr><td class='co'>{esc((r+['','',''])[0])}</td><td>{esc((r+['','',''])[1])}</td>"
        f"<td class='nt'>{esc((r+['','',''])[2])}</td></tr>"
        for r in skipped if r and r[0]
    )
    prep_html = "".join(
        f"<details class='qa'><summary>{esc((r+['',''])[0])}</summary><p>{esc((r+['',''])[1])}</p></details>"
        for r in prep if r and r[0]
    )
    answers_html = "".join(
        f"<details class='qa'><summary><b>{esc((r+['','',''])[0])}</b> — {esc((r+['','',''])[1])}</summary>"
        f"<p>{esc((r+['','',''])[2])}</p></details>"
        for r in answers if r and r[0]
    )

    doc = TEMPLATE.format(
        generated=GENERATED, owner=OWNER,
        total=summary["total"], interviewing=summary["interviewing"], applied=summary["applied"],
        screening=summary["screening"], rejected=summary["rejected"], filled=summary["filled"],
        companies=summary["companies"], active_cards="".join(active_cards),
        rows="".join(rows_html), skipped=skipped_html, prep=prep_html, answers=answers_html,
        archive_answers=archive_answers_html(),
    )
    OUT.write_text(doc, encoding="utf-8")
    print(f"wrote {OUT}  ({summary['total']} applications across {summary['companies']} companies)")


def _ord(datestr: str) -> int:
    """rough sortable int from 'YYYY-MM-DD' or 'May 2026' (recent = bigger)."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", datestr or "")
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    months = dict(jan=1, feb=2, mar=3, apr=4, may=5, jun=6, jul=7, aug=8, sep=9, oct=10, nov=11, dec=12)
    m2 = re.search(r"([A-Za-z]{3}).*?(\d{4})", datestr or "")
    if m2:
        return int(m2.group(2)) * 10000 + months.get(m2.group(1).lower(), 0) * 100
    return 0


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{owner} — Job Search Dashboard</title>
<style>
  :root{{--bg:#0f1115;--card:#181b22;--line:#262b36;--fg:#e7eaf0;--mut:#9aa3b2;--accent:#5b8cff}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
  .wrap{{max-width:1280px;margin:0 auto;padding:28px 20px 80px}}
  h1{{font-size:22px;margin:0 0 2px}} .sub{{color:var(--mut);margin:0 0 20px;font-size:13px}}
  .cards{{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;margin-bottom:18px}}
  @media(max-width:820px){{.cards{{grid-template-columns:repeat(3,1fr)}}}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
  .card .n{{font-size:26px;font-weight:700}} .card .l{{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
  .banner{{background:linear-gradient(90deg,#3a2a00,#2a2410);border:1px solid #6b5310;border-radius:12px;padding:14px 18px;margin-bottom:18px}}
  .banner b{{color:#ffd866}}
  .controls{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}}
  input[type=search]{{background:var(--card);border:1px solid var(--line);color:var(--fg);border-radius:9px;padding:9px 12px;min-width:240px;flex:1}}
  .fbtn{{background:var(--card);border:1px solid var(--line);color:var(--mut);border-radius:999px;padding:7px 14px;cursor:pointer;font-size:13px}}
  .fbtn.active{{background:var(--accent);border-color:var(--accent);color:#fff}}
  table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
  th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}}
  th{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);cursor:pointer;user-select:none;position:sticky;top:0;background:#1b1f27}}
  tr:hover{{background:#1d212a}} td.co{{font-weight:600;white-space:nowrap}} td.loc,td.dt,td.sal{{color:var(--mut);white-space:nowrap}}
  td.nt{{color:var(--mut);font-size:12.5px;max-width:320px}}
  td.jd details summary{{cursor:pointer;color:var(--accent);font-size:12px;white-space:nowrap}}
  td.jd pre{{white-space:pre-wrap;font:12px/1.45 ui-monospace,Menlo,monospace;color:var(--fg);background:#0c0e12;border:1px solid var(--line);border-radius:8px;padding:10px;max-width:520px;max-height:340px;overflow:auto;margin:6px 0 0}}
  a{{color:var(--accent);text-decoration:none}} a:hover{{text-decoration:underline}}
  .pill{{display:inline-block;padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}}
  .pill.applied{{background:#10331f;color:#5fd38a}} .pill.rejected{{background:#3a1620;color:#ff7a93}}
  .pill.screening{{background:#3a2e00;color:#ffd866}} .pill.filled{{background:#102a3a;color:#5bb8ff}} .pill.other{{background:#262b36;color:var(--mut)}}
  .pill.interviewing{{background:#0c3a2a;color:#3ce0a0;box-shadow:0 0 0 1px #1c6e4f inset}}
  .src{{font-size:11px;padding:2px 7px;border-radius:6px}} .src-cowork{{background:#241a33;color:#c39bff}} .src-honestapply{{background:#10282a;color:#5fd3c9}} .src-active{{background:#0c3a2a;color:#3ce0a0}}
  .active-wrap{{margin-bottom:20px}} .active-wrap h2{{margin:0 0 10px}}
  .act{{background:var(--card);border:1px solid var(--line);border-left:3px solid #3ce0a0;border-radius:10px;padding:11px 14px;margin-bottom:9px}}
  .act-screening{{border-left-color:#ffd866}}
  .act-h{{font-size:14px}} .act-m{{color:var(--mut);font-size:12.5px;margin:3px 0}} .act-n{{color:#ffd866;font-size:13px;margin-top:4px}}
  h2{{font-size:16px;margin:34px 0 10px}} details.qa{{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:8px 12px;margin:6px 0}}
  details.qa summary{{cursor:pointer;color:var(--fg)}} details.qa p{{color:var(--mut);margin:8px 0 2px;white-space:pre-wrap}}
  .muted{{color:var(--mut)}} .hide{{display:none}}
</style></head><body><div class="wrap">
  <h1>{owner} — Job Search Dashboard</h1>
  <p class="sub">One place for everything · merges the autonomous <b>honestapply</b> pipeline + the <b>cowork</b> Excel tracker · job descriptions archived locally (survive page takedown) · every submitted answer saved in <code>data/application_archive/</code> · generated {generated}</p>

  <div class="cards">
    <div class="card"><div class="n">{total}</div><div class="l">Applications</div></div>
    <div class="card"><div class="n">{companies}</div><div class="l">Companies</div></div>
    <div class="card"><div class="n" style="color:#3ce0a0">{interviewing}</div><div class="l">Interviewing</div></div>
    <div class="card"><div class="n" style="color:#ffd866">{screening}</div><div class="l">Screening</div></div>
    <div class="card"><div class="n" style="color:#5fd38a">{applied}</div><div class="l">Applied</div></div>
    <div class="card"><div class="n" style="color:#5bb8ff">{filled}</div><div class="l">Filled·not sent</div></div>
    <div class="card"><div class="n" style="color:#ff7a93">{rejected}</div><div class="l">Rejected</div></div>
  </div>

  <div class="active-wrap">
    <h2>🔥 Active pipeline — next actions</h2>
    {active_cards}
  </div>

  <div class="controls">
    <input id="q" type="search" placeholder="Search company, role, location…">
    <button class="fbtn active" data-f="all">All</button>
    <button class="fbtn" data-f="interviewing">Interviewing</button>
    <button class="fbtn" data-f="screening">Screening</button>
    <button class="fbtn" data-f="applied">Applied</button>
    <button class="fbtn" data-f="filled">Filled</button>
    <button class="fbtn" data-f="rejected">Rejected</button>
    <button class="fbtn" data-f="cowork">cowork</button>
    <button class="fbtn" data-f="honestapply">honestapply</button>
  </div>

  <table id="tbl"><thead><tr>
    <th data-k="0">Company</th><th data-k="1">Role</th><th data-k="2">Location</th>
    <th data-k="3">Source</th><th data-k="4">Status</th><th data-k="5">Applied</th>
    <th>JD (archived)</th><th data-k="7">Notes</th>
  </tr></thead><tbody>{rows}</tbody></table>

  <h2>Skipped roles (deliberate no-go) <span class="muted">— from cowork tracker</span></h2>
  <table><thead><tr><th>Company</th><th>Role</th><th>Reason for skipping</th></tr></thead><tbody>{skipped}</tbody></table>

  <h2>Interview prep <span class="muted">— ready answers</span></h2>
  {prep}

  <h2>Submitted application answers <span class="muted">— reuse these</span></h2>
  {answers}

  <h2>Archived form answers <span class="muted">— everything typed into real forms (data/application_archive/)</span></h2>
  {archive_answers}

<script>
  const q=document.getElementById('q'), tb=document.querySelector('#tbl tbody');
  let curF='all';
  function apply(){{
    const term=q.value.trim().toLowerCase();
    for(const tr of tb.rows){{
      const okF = curF==='all' || tr.dataset.status===curF || tr.dataset.source===curF;
      const okT = !term || tr.dataset.text.includes(term);
      tr.classList.toggle('hide', !(okF&&okT));
    }}
  }}
  q.addEventListener('input', apply);
  document.querySelectorAll('.fbtn').forEach(b=>b.addEventListener('click',()=>{{
    document.querySelectorAll('.fbtn').forEach(x=>x.classList.remove('active'));
    b.classList.add('active'); curF=b.dataset.f; apply();
  }}));
  document.querySelectorAll('#tbl th').forEach(th=>th.addEventListener('click',()=>{{
    const k=+th.dataset.k, rows=[...tb.rows];
    const asc=th._asc=!th._asc;
    rows.sort((a,b)=>{{const x=a.cells[k].innerText.trim(),y=b.cells[k].innerText.trim();
      return asc? x.localeCompare(y): y.localeCompare(x);}});
    rows.forEach(r=>tb.appendChild(r));
  }}));
</script>
</div></body></html>
"""

if __name__ == "__main__":
    main()
