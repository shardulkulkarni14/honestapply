"""Teamtailor career-site JSON feed fetcher.

Public endpoint (no auth required; documented alongside the ``jobs.rss`` feed):
    GET https://{token}.teamtailor.com/jobs.json
    GET https://{custom career host}/jobs.json

Employer options (``employers.yaml``):
    token : the career-site sub-domain, e.g. ``acme`` for acme.teamtailor.com
    host  : full host when the company uses a custom career domain

Field mapping (JSON Feed item -> Job):
    id              -> external_id
    title           -> title
    _jobposting.jobLocation (schema.org) -> location; jobLocationType TELECOMMUTE -> "Remote"
    url             -> url
    content_html    -> description (HTML stripped)
    date_published  -> posted_at
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import requests

from honestapply.ats._html import strip_html
from honestapply.logging_setup import get_logger

if TYPE_CHECKING:
    from honestapply.config import Employer

log = get_logger(__name__)

_TIMEOUT = 20
_HEADERS = {"User-Agent": "honestapply/1.0 (+https://github.com/honestapply)"}


def feed_host(employer: "Employer") -> str:
    host = str(getattr(employer, "host", "") or "").strip().lower()
    return host or f"{employer.board_token()}.teamtailor.com"


def _location_from_posting(posting: Any) -> str:
    """Flatten schema.org ``jobLocation`` (dict or list) to "City, Country"."""
    if not isinstance(posting, dict):
        return ""
    locs = posting.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    names: list[str] = []
    for loc in locs:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, str):
            names.append(addr)
            continue
        city = addr.get("addressLocality") or ""
        country = addr.get("addressCountry") or ""
        if isinstance(country, dict):
            country = country.get("name") or ""
        part = ", ".join(p for p in (city, country) if p)
        if part:
            names.append(part)
    location = "; ".join(dict.fromkeys(names))
    if str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        location = f"Remote, {location}" if location else "Remote"
    return location


def parse_feed(payload: dict, company: str) -> list[dict]:
    jobs: list[dict] = []
    for item in payload.get("items") or []:
        title = (item.get("title") or "").strip()
        url = item.get("url") or ""
        if not title or not url:
            continue
        posting = item.get("_jobposting") or {}
        jobs.append(
            {
                "company": company,
                "title": title,
                "location": _location_from_posting(posting),
                "url": url,
                "external_id": str(item.get("id") or ""),
                "description": strip_html(item.get("content_html")) or None,
                "source_board": "teamtailor",
                "_posted_at": item.get("date_published"),
            }
        )
    return jobs


def fetch_jobs(employer: "Employer") -> list[dict]:
    host = feed_host(employer)
    url = f"https://{host}/jobs.json"
    log.info("teamtailor.fetch", company=employer.name, host=host)
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        payload = resp.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        log.warning("teamtailor.http_error", company=employer.name, status=status, url=url)
        return []
    except (requests.RequestException, ValueError) as exc:
        log.warning("teamtailor.request_error", company=employer.name, error=str(exc))
        return []
    jobs = parse_feed(payload if isinstance(payload, dict) else {}, employer.name)
    log.info("teamtailor.done", company=employer.name, count=len(jobs))
    return jobs
