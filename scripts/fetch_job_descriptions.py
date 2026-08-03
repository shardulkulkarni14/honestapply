"""Archive the verbatim job description for every application — locally, so it
survives the company taking the posting down.

For each Job that has an apply URL, hit the ATS's public JSON API (Ashby /
Greenhouse / Lever) or fetch the Personio page, store the full JD text into
  - jobs.description   (the DB)
  - data/application_archive/jd/<id>_<slug>.md   (a durable local copy)

Stdlib only. Re-run any time:  python scripts/fetch_job_descriptions.py
Add --all to refetch even where a JD already exists.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import ssl
import sys
import urllib.request
from pathlib import Path

# TLS verification stays ON. macOS Python.framework builds often ship without a
# usable CA bundle, which is what tempts you to disable it — use certifi instead
# when it is available, and otherwise fall back to the system trust store.
try:
    import certifi

    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover - depends on the install extras
    _SSL = ssl.create_default_context()

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "honestapply.db"
JD_DIR = ROOT / "data" / "application_archive" / "jd"
UA = {"User-Agent": "Mozilla/5.0 (archive-bot; personal job tracker)"}


def get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
        return r.read().decode("utf-8", "ignore")


def strip_html(s: str) -> str:
    s = re.sub(r"<\s*(br|/p|/div|/li|/h\d)\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:50]


def fetch_jd(url: str) -> str | None:
    try:
        # ---- Ashby ----
        m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)
        if m:
            org, jid = m.group(1), m.group(2)
            data = json.loads(get(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"))
            for j in data.get("jobs", []):
                if j.get("jobId") == jid or j.get("id") == jid:
                    txt = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", ""))
                    loc = j.get("location", "")
                    comp = j.get("compensation", {})
                    return f"[{j.get('title')}] · {loc}\n\n{txt}".strip()
            return None
        # ---- Greenhouse ----
        m = re.search(r"greenhouse\.io/([^/?]+)/jobs/(\d+)", url)
        if m:
            org, jid = m.group(1), m.group(2)
            for base in ("boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"):
                try:
                    d = json.loads(get(f"https://{base}/v1/boards/{org}/jobs/{jid}"))
                    return f"[{d.get('title')}] · {d.get('location',{}).get('name','')}\n\n{strip_html(d.get('content',''))}".strip()
                except Exception:
                    continue
            return None
        # ---- Lever ----
        m = re.search(r"lever\.co/([^/]+)/([0-9a-f-]{36})", url)
        if m:
            org, jid = m.group(1), m.group(2)
            d = json.loads(get(f"https://api.lever.co/v0/postings/{org}/{jid}"))
            parts = [d.get("text", ""), strip_html(d.get("description", ""))]
            for lst in d.get("lists", []):
                parts.append(f"\n## {strip_html(lst.get('text',''))}\n{strip_html(lst.get('content',''))}")
            parts.append(strip_html(d.get("additional", "")))
            return "\n".join(p for p in parts if p).strip()
        # ---- Personio ----
        m = re.search(r"(https://[^/]+\.jobs\.personio\.[a-z]+)/job/(\d+)", url)
        if m:
            page = get(f"{m.group(1)}/job/{m.group(2)}?language=en")
            body = re.search(r'<div[^>]*class="[^"]*job-description[^"]*"[^>]*>(.*?)</div>\s*</div>', page, re.S)
            if not body:
                body = re.search(r"<main[^>]*>(.*?)</main>", page, re.S)
            return strip_html(body.group(1)) if body else strip_html(page)[:6000]
    except Exception as e:
        return f"__ERROR__ {e}"
    return None


def main() -> None:
    refetch_all = "--all" in sys.argv
    JD_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT DISTINCT j.id, j.company, j.title, j.url, j.description
        FROM jobs j JOIN applications a ON a.job_id=j.id
        WHERE j.url IS NOT NULL AND j.url<>''
    """).fetchall()

    ok = skip = fail = 0
    for r in rows:
        has = r["description"] and len(r["description"]) > 80
        if has and not refetch_all:
            skip += 1
            continue
        jd = fetch_jd(r["url"])
        if not jd or jd.startswith("__ERROR__") or len(jd) < 80:
            fail += 1
            print(f"  ✗ {r['company']} — {(r['title'] or '')[:40]}  ({(jd or 'no api match')[:60]})")
            continue
        con.execute("UPDATE jobs SET description=? WHERE id=?", (jd, r["id"]))
        fn = JD_DIR / f"{r['id']}_{slug(r['company'])}_{slug(r['title'])}.md"
        fn.write_text(
            f"# {r['company']} — {r['title']}\n\nSource: {r['url']}\nArchived: 2026-06-09\n\n---\n\n{jd}\n",
            encoding="utf-8",
        )
        ok += 1
        print(f"  ✓ {r['company']} — {(r['title'] or '')[:40]}  ({len(jd)} chars)")
    con.commit()
    con.close()
    print(f"\nfetched {ok} · skipped {skip} (already had JD) · failed {fail} → {JD_DIR}")


if __name__ == "__main__":
    main()
