"""Instahyre — India job-board discovery fetcher.

WHY THIS EXISTS (2026-08-17)
    India supply through company ATS boards is exhausted. Measured over 47
    rounds / 3 days: 575 India jobs discovered -> 3 passed the fit gate -> 2
    real submissions (0.35% end-to-end). 33 of those 47 rounds found *zero*
    live India candidates. The company-board route cannot reach 30 India
    applications on any useful horizon.

    Of the four India-specific boards evaluated:
      - Naukri    -> API returns {"message": "recaptcha required"} (HTTP 406).
                     honestapply never solves CAPTCHAs, so it is not usable.
      - Wellfound -> HTTP 403 (Cloudflare) on unauthenticated requests.
      - Cutshort  -> HTML only, no JSON API; scrapeable but brittle.
      - Instahyre -> clean public JSON API, no auth. This module.

UNLIKE the other fetchers in this package, Instahyre is an AGGREGATOR, not a
single company's board: one employer entry yields postings across many
companies, so each job dict carries its own `company` taken from the payload
rather than the employer name.

API shape
    GET https://www.instahyre.com/api/v1/job_search?limit=35&offset=N
    - `offset` paginates; `limit`, `q`, `keywords` and `location` are IGNORED by
      the endpoint (it always returns 35 rows), so all filtering below is done
      client-side. Do not "fix" this by adding query params — they silently
      do nothing and make the code look like it filters when it does not.
    - meta.total_count was 13,969 at time of writing (1,479 Data Science/ML;
      7,368 Bangalore, 1,478 Hyderabad).

KNOWN LIMIT — descriptions
    Job detail pages (`public_url`) return HTTP 403 to plain HTTP clients
    (Cloudflare bot protection), so the enrich stage cannot backfill a
    description the usual way. We therefore seed `description` with the
    posting's OWN structured fields (title, company, location, skill keywords)
    straight from the API. That is real posting data, never invented text — the
    no-fabrication rule applies to this seed exactly as it does everywhere else.

APPLY SIDE — READ BEFORE EXPECTING SUBMISSIONS
    Instahyre's apply button requires a logged-in candidate account. Discovery
    here is unauthenticated and works standalone, but jobs will route to
    `needs_human` unless a persisted Instahyre session exists in
    settings.browser_profile_dir. See DECISIONS.md.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import requests

from honestapply.logging_setup import get_logger

if TYPE_CHECKING:
    from honestapply.config import Employer

log = get_logger(__name__)

_API = "https://www.instahyre.com/api/v1/job_search"
_TIMEOUT = 20
# A browser-ish UA is required: the default python-requests UA gets 403ed.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
# The endpoint returns 35 rows per call no matter what `limit` says.
_PAGE = 35
# Hard ceiling on rows pulled per run. The board carries ~14k postings; pulling
# all of them would be ~400 sequential requests for a pool the fit gate would
# discard anyway. The title filter below is what actually decides relevance.
_MAX_ROWS = 1400
# Politeness delay between pages — this is an unauthenticated public endpoint
# and we are not entitled to hammer it. Measured 2026-08-17: 0.5s got a 429 at
# offset 700 (20 pages in ~21s), so the endpoint throttles at roughly 1 req/s.
_PAGE_DELAY_SECONDS = 1.5
# On 429 the run used to stop dead at whatever offset it had reached, silently
# capping the pool. Back off and retry instead.
_MAX_RETRIES_PER_PAGE = 3
_BACKOFF_BASE_SECONDS = 20

# Titles worth surfacing for an applied-AI/GenAI profile. Deliberately broader
# than the final scoring gate: this only decides what enters the pool, and the
# LLM score stage still judges every one of them.
_RELEVANT_TITLE = re.compile(
    r"\b(ai|a\.i\.|ml|genai|gen ai|llm|nlp|machine learning|deep learning|"
    r"data scien|applied scien|mlops|agentic|rag|computer vision|"
    r"backend|back-end|python|platform engineer|software engineer|sde|"
    r"full[ -]?stack|architect|staff engineer|principal engineer)\b",
    re.IGNORECASE,
)


def _description_seed(row: dict, company: str) -> str:
    """Build a description from the posting's OWN fields.

    Instahyre job pages are Cloudflare-protected, so enrich cannot fetch the
    real body. Everything here is copied verbatim from the API payload — no
    invented duties, no inferred requirements.
    """
    title = (row.get("title") or "").strip()
    locations = (row.get("locations") or "").strip()
    keywords = [str(k).strip() for k in (row.get("keywords") or []) if str(k).strip()]
    employer = row.get("employer") or {}
    note = (employer.get("instahyre_note") or "").strip()

    parts = [f"{title} at {company}."]
    if locations:
        parts.append(f"Location: {locations}.")
    if keywords:
        parts.append("Skills listed on the posting: " + ", ".join(keywords) + ".")
    if note:
        parts.append(f"About {company} (from the posting): {note}")
    parts.append(
        "[Sourced from Instahyre's public listing API; the full job body is not "
        "retrievable because the posting page is bot-protected.]"
    )
    return " ".join(parts)


def fetch_jobs(employer: "Employer") -> list[dict]:
    """Page Instahyre's public job feed and return discover-compatible dicts.

    Optional employer fields (Employer allows extras):
      `max_rows`     int  — override the per-run row ceiling (default 1400)
      `all_titles`   bool — skip the relevance title filter and return everything
    """
    max_rows = int(getattr(employer, "max_rows", _MAX_ROWS) or _MAX_ROWS)
    all_titles = bool(getattr(employer, "all_titles", False))

    jobs: list[dict] = []
    seen: set[str] = set()
    offset = 0
    scanned = 0
    total = None

    while offset < max_rows:
        resp = None
        for attempt in range(_MAX_RETRIES_PER_PAGE):
            try:
                resp = requests.get(
                    _API,
                    params={"limit": _PAGE, "offset": offset},
                    timeout=_TIMEOUT,
                    headers=_HEADERS,
                )
                resp.raise_for_status()
                break
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                resp = None
                # 429 is a throttle, not a refusal: wait longer and try again.
                # Anything else (403/5xx) is not worth retrying against a public
                # endpoint we do not control.
                if status == 429 and attempt < _MAX_RETRIES_PER_PAGE - 1:
                    wait = _BACKOFF_BASE_SECONDS * (attempt + 1)
                    log.info("instahyre.throttled", offset=offset, wait_seconds=wait)
                    time.sleep(wait)
                    continue
                log.warning("instahyre.http_error", status=status, offset=offset)
                break
            except requests.RequestException as exc:
                resp = None
                log.warning("instahyre.request_error", error=str(exc), offset=offset)
                break
        if resp is None:
            break

        data = resp.json() if resp.content else {}
        rows = data.get("objects") or []
        if total is None:
            total = (data.get("meta") or {}).get("total_count")
            log.info("instahyre.fetch", total=total, max_rows=max_rows)
        if not rows:
            break

        for row in rows:
            scanned += 1
            job_id = str(row.get("id") or "")
            url = (row.get("public_url") or "").strip()
            title = (row.get("title") or "").strip()
            if not job_id or not url or not title:
                continue
            # The feed repeats postings across pages; dedupe within the run so a
            # repeated page does not inflate the pool.
            if job_id in seen:
                continue
            seen.add(job_id)

            if not all_titles and not _RELEVANT_TITLE.search(title):
                continue

            company = ((row.get("employer") or {}).get("company_name") or "").strip()
            if not company:
                continue

            jobs.append(
                {
                    "company": company,
                    "title": title,
                    "location": (row.get("locations") or "").strip(),
                    "url": url,
                    "external_id": job_id,
                    "description": _description_seed(row, company),
                    "source_board": "instahyre",
                    "_posted_at": None,  # not exposed by this endpoint
                }
            )

        offset += _PAGE
        if total is not None and offset >= total:
            break
        time.sleep(_PAGE_DELAY_SECONDS)

    log.info("instahyre.done", scanned=scanned, kept=len(jobs), unique=len(seen))
    return jobs
