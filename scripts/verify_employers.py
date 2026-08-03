"""Probe candidate ATS board tokens and report which ones actually resolve.

Adding a guessed token to employers.yaml is worse than not adding it: discovery
silently fetches nothing and the pool looks healthy while staying empty. This
hits each board's public API and prints only what really returns postings, so
employers.yaml only ever gains verified entries.

    python scripts/verify_employers.py candidates.tsv

Input is TSV: name<TAB>ats<TAB>token   (ats = greenhouse | lever | ashby)
Output is YAML ready to paste into config/employers.yaml, plus a failure list.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

import httpx

TIMEOUT = 12.0

# Public, unauthenticated job-board endpoints for each ATS.
URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
    # honestapply already has a working SmartRecruiters fetcher
    # (src/honestapply/ats/smartrecruiters.py), and many India-HQ employers use it
    # rather than Greenhouse/Lever/Ashby — so verifying these tokens is the
    # cheapest route to more Indian supply.
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100",
}


def count_jobs(ats: str, payload) -> int:
    if ats == "greenhouse":
        return len((payload or {}).get("jobs", []))
    if ats == "lever":
        return len(payload or [])
    if ats == "ashby":
        return len((payload or {}).get("jobs", []))
    if ats == "smartrecruiters":
        return int((payload or {}).get("totalFound") or len((payload or {}).get("content", [])))
    return 0


def probe(entry: tuple[str, str, str]) -> dict:
    name, ats, token = entry
    url = URLS[ats].format(t=token)
    try:
        r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return {"name": name, "ats": ats, "token": token, "ok": False,
                    "why": f"HTTP {r.status_code}"}
        n = count_jobs(ats, r.json())
        # A board that resolves but lists nothing is not worth sourcing from.
        return {"name": name, "ats": ats, "token": token, "ok": n > 0,
                "n": n, "why": "" if n else "0 jobs"}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ats": ats, "token": token, "ok": False,
                "why": type(exc).__name__}


def main() -> None:
    path = sys.argv[1]
    entries = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            name, ats, token = (p.strip() for p in parts)
            if ats in URLS:
                entries.append((name, ats, token))

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(probe, entries))

    good = sorted([r for r in results if r["ok"]], key=lambda r: -r["n"])
    bad = [r for r in results if not r["ok"]]

    print(f"# verified {len(good)}/{len(results)} boards\n")
    for r in good:
        print(f"  - name: {r['name']}")
        print(f"    ats: {r['ats']}")
        print(f"    token: {r['token']}   # verified: ~{r['n']} jobs")
    print(f"\n# FAILED ({len(bad)}):")
    for r in bad:
        print(f"#   {r['name']:28} {r['ats']:11} {r['token']:26} {r['why']}")


if __name__ == "__main__":
    main()
