"""Lever public postings API fetcher.

Public endpoint (no auth required):
    GET https://api.lever.co/v0/postings/{token}?mode=json

Returns a list of dicts compatible with the discover stage's Job insertion.

Field mapping (Lever v0 -> Job):
    id           -> external_id
    text         -> title
    categories.location -> location
    hostedUrl    -> url
    createdAt    -> posted_at  (epoch ms)
    descriptionPlain / description -> description
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import requests

from honestapply.logging_setup import get_logger

if TYPE_CHECKING:
    from honestapply.config import Employer

log = get_logger(__name__)

_BASE = "https://api.lever.co/v0/postings"
_TIMEOUT = 20
_HEADERS = {"User-Agent": "honestapply/1.0 (+https://github.com/honestapply)"}


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    for ent, ch in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(ent, ch)
    return " ".join(text.split())


def _epoch_ms_to_datetime(ms: int | None) -> datetime | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (ValueError, OSError):
        return None


def fetch_jobs(employer: "Employer") -> list[dict]:
    """Fetch all jobs from a Lever public board.

    Each returned dict has at minimum:
        company, title, location, url, external_id, description, source_board
    """
    token = employer.board_token()
    url = f"{_BASE}/{token}?mode=json"
    log.info("lever.fetch", company=employer.name, token=token, url=url)

    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        log.warning("lever.http_error", company=employer.name, status=exc.response.status_code if exc.response else None, url=url)
        return []
    except requests.RequestException as exc:
        log.warning("lever.request_error", company=employer.name, error=str(exc))
        return []

    raw = resp.json()
    if not isinstance(raw, list):
        log.warning("lever.unexpected_shape", company=employer.name, type=type(raw).__name__)
        return []

    jobs: list[dict] = []
    for item in raw:
        categories = item.get("categories") or {}
        location = categories.get("location") or categories.get("allLocations", [""])[0] if categories.get("allLocations") else ""

        # Prefer plain text description; fall back to HTML-stripped
        description = item.get("descriptionPlain") or _strip_html(item.get("description", ""))

        posted_at = _epoch_ms_to_datetime(item.get("createdAt"))

        jobs.append(
            {
                "company": employer.name,
                "title": (item.get("text") or "").strip(),
                "location": location,
                "url": item.get("hostedUrl", ""),
                "external_id": str(item.get("id", "")),
                "description": description,
                "source_board": "lever",
                "_commitment": categories.get("commitment", ""),
                "_team": categories.get("team", ""),
                "_posted_at": posted_at,
            }
        )

    log.info("lever.done", company=employer.name, count=len(jobs))
    return jobs
