"""Ashby public job board API fetcher.

Public endpoint (no auth required):
    GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true

The token is the org slug from jobs.ashbyhq.com/{slug}

Field mapping (Ashby -> Job):
    id           -> external_id
    title        -> title
    location / address -> location
    jobUrl       -> url
    publishedAt  -> posted_at
    descriptionHtml / descriptionPlain -> description
    compensation -> salary_range_text
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import requests

from honestapply.logging_setup import get_logger

if TYPE_CHECKING:
    from honestapply.config import Employer

log = get_logger(__name__)

_BASE = "https://api.ashbyhq.com/posting-api/job-board"
_TIMEOUT = 20
_HEADERS = {"User-Agent": "honestapply/1.0 (+https://github.com/honestapply)"}


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    for ent, ch in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(ent, ch)
    return " ".join(text.split())


def _extract_salary(compensation: dict | None) -> str:
    """Pull a human-readable salary summary from Ashby's compensation object."""
    if not compensation:
        return ""
    tier_summary = compensation.get("compensationTierSummary") or ""
    if tier_summary:
        return tier_summary
    scrapeable = compensation.get("scrapeableCompensationSalarySummary") or ""
    if scrapeable:
        return scrapeable
    return ""


def fetch_jobs(employer: "Employer") -> list[dict]:
    """Fetch all jobs from an Ashby public board.

    Each returned dict has at minimum:
        company, title, location, url, external_id, description, source_board
    """
    token = employer.board_token()
    url = f"{_BASE}/{token}?includeCompensation=true"
    log.info("ashby.fetch", company=employer.name, token=token, url=url)

    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        log.warning("ashby.http_error", company=employer.name, status=exc.response.status_code if exc.response else None, url=url)
        return []
    except requests.RequestException as exc:
        log.warning("ashby.request_error", company=employer.name, error=str(exc))
        return []

    data = resp.json()
    raw: list[dict] = data.get("jobs", [])

    jobs: list[dict] = []
    for item in raw:
        # Skip unlisted postings
        if not item.get("isListed", True):
            continue

        # Location: prefer the top-level `location` field; fall back to `address`
        location = item.get("location") or ""
        if not location:
            addr = item.get("address") or {}
            location = addr.get("city") or addr.get("postalAddress", {}).get("addressLocality") or ""

        # Description: prefer plain text
        description = item.get("descriptionPlain") or _strip_html(item.get("descriptionHtml", ""))

        salary = _extract_salary(item.get("compensation"))

        jobs.append(
            {
                "company": employer.name,
                "title": (item.get("title") or "").strip(),
                "location": location,
                "url": item.get("jobUrl", ""),
                "external_id": str(item.get("id", "")),
                "description": description,
                "source_board": "ashby",
                "salary_range_text": salary,
                "_published_at": item.get("publishedAt", ""),
                "_is_remote": item.get("isRemote", False),
                "_employment_type": item.get("employmentType", ""),
                "_department": item.get("department", ""),
            }
        )

    log.info("ashby.done", company=employer.name, count=len(jobs))
    return jobs
