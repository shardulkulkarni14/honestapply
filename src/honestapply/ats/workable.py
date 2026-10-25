"""Workable public widget API fetcher.

Public endpoint (no auth required):
    GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true

Field mapping (Workable job -> Job):
    shortcode     -> external_id
    title         -> title
    city, state, country (+ "Remote" when telecommuting) -> location
    url           -> url (the apply.workable.com posting)
    description + requirements (HTML, only with details=true) -> description
    published_on  -> posted_at
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import requests

from honestapply.ats._html import strip_html
from honestapply.logging_setup import get_logger

if TYPE_CHECKING:
    from honestapply.config import Employer

log = get_logger(__name__)

_BASE = "https://apply.workable.com/api/v1/widget/accounts"
_TIMEOUT = 20
_HEADERS = {"User-Agent": "honestapply/1.0 (+https://github.com/honestapply)"}


def parse_widget(payload: dict, company: str) -> list[dict]:
    jobs: list[dict] = []
    for item in payload.get("jobs") or []:
        title = (item.get("title") or "").strip()
        url = item.get("url") or item.get("shortlink") or ""
        if not title or not url:
            continue
        parts = [item.get("city"), item.get("state"), item.get("country")]
        location = ", ".join(p for p in parts if p)
        if item.get("telecommuting") or str(item.get("workplace") or "").lower() == "remote":
            location = f"Remote, {location}" if location else "Remote"
        description = " ".join(
            part for part in (strip_html(item.get("description")), strip_html(item.get("requirements"))) if part
        )
        jobs.append(
            {
                "company": payload.get("name") or company,
                "title": title,
                "location": location,
                "url": url,
                "external_id": str(item.get("shortcode") or item.get("code") or ""),
                "description": description or None,
                "source_board": "workable",
                "_department": item.get("department") or "",
                "_employment_type": item.get("employment_type") or "",
                "_posted_at": item.get("published_on") or item.get("created_at"),
            }
        )
    return jobs


def fetch_jobs(employer: "Employer") -> list[dict]:
    token = employer.board_token()
    url = f"{_BASE}/{token}"
    log.info("workable.fetch", company=employer.name, token=token)
    try:
        resp = requests.get(url, params={"details": "true"}, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        payload = resp.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        log.warning("workable.http_error", company=employer.name, status=status, url=url)
        return []
    except (requests.RequestException, ValueError) as exc:
        log.warning("workable.request_error", company=employer.name, error=str(exc))
        return []
    jobs = parse_widget(payload if isinstance(payload, dict) else {}, employer.name)
    log.info("workable.done", company=employer.name, count=len(jobs))
    return jobs
