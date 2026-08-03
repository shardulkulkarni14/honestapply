"""Greenhouse public board API fetcher.

Public endpoint (no auth required):
    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true

Returns a list of dicts compatible with the discover stage's Job insertion.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import requests

from honestapply.logging_setup import get_logger

if TYPE_CHECKING:
    from honestapply.config import Employer

log = get_logger(__name__)

_BASE = "https://boards-api.greenhouse.io/v1/boards"
_TIMEOUT = 20
_HEADERS = {"User-Agent": "honestapply/1.0 (+https://github.com/honestapply)"}


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode basic entities."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    # decode common HTML entities
    for ent, ch in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(ent, ch)
    return " ".join(text.split())  # collapse whitespace


def fetch_jobs(employer: "Employer") -> list[dict]:
    """Fetch all jobs from a Greenhouse public board.

    Each returned dict has at minimum:
        company, title, location, url, external_id, description, source_board
    """
    token = employer.board_token()
    url = f"{_BASE}/{token}/jobs?content=true"
    log.info("greenhouse.fetch", company=employer.name, token=token, url=url)

    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        log.warning("greenhouse.http_error", company=employer.name, status=exc.response.status_code if exc.response else None, url=url)
        return []
    except requests.RequestException as exc:
        log.warning("greenhouse.request_error", company=employer.name, error=str(exc))
        return []

    raw: list[dict] = resp.json().get("jobs", [])
    jobs: list[dict] = []

    for item in raw:
        loc = item.get("location") or {}
        location = loc.get("name", "") if isinstance(loc, dict) else str(loc)

        # Greenhouse content field is HTML
        description_html = item.get("content", "") or ""
        description = _strip_html(description_html)

        # Salary — Greenhouse doesn't include it in the board API; leave blank
        jobs.append(
            {
                "company": employer.name,
                "title": (item.get("title") or "").strip(),
                "location": location,
                "url": item.get("absolute_url", ""),
                "external_id": str(item.get("id", "")),
                "description": description,
                "source_board": "greenhouse",
                # Extra enrichment hints
                "_departments": [d.get("name", "") for d in (item.get("departments") or [])],
                "_updated_at": item.get("updated_at", ""),
                "_first_published": item.get("first_published", ""),
            }
        )

    log.info("greenhouse.done", company=employer.name, count=len(jobs))
    return jobs
