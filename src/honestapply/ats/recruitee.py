"""Recruitee public offers API fetcher.

Public endpoint (no auth required):
    GET https://{token}.recruitee.com/api/offers/

Field mapping (Recruitee offer -> Job):
    id            -> external_id
    title         -> title
    location / city, country -> location ("Remote" prefixed when remote)
    careers_url   -> url
    description + requirements (HTML) -> description
    published_at  -> posted_at
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import requests

from honestapply.ats._html import strip_html
from honestapply.logging_setup import get_logger

if TYPE_CHECKING:
    from honestapply.config import Employer

log = get_logger(__name__)

_TIMEOUT = 20
_HEADERS = {"User-Agent": "honestapply/1.0 (+https://github.com/honestapply)"}


def parse_offers(payload: dict, company: str) -> list[dict]:
    jobs: list[dict] = []
    for offer in payload.get("offers") or []:
        title = (offer.get("title") or "").strip()
        url = offer.get("careers_url") or ""
        if not title or not url:
            continue
        location = (offer.get("location") or "").strip()
        if not location:
            location = ", ".join(p for p in (offer.get("city"), offer.get("country")) if p)
        if offer.get("remote") and "remote" not in location.lower():
            location = f"Remote, {location}" if location else "Remote"
        description = " ".join(
            part for part in (strip_html(offer.get("description")), strip_html(offer.get("requirements"))) if part
        )
        jobs.append(
            {
                "company": offer.get("company_name") or company,
                "title": title,
                "location": location,
                "url": url,
                "external_id": str(offer.get("id") or offer.get("slug") or ""),
                "description": description or None,
                "source_board": "recruitee",
                "_department": offer.get("department") or "",
                "_posted_at": offer.get("published_at") or offer.get("created_at"),
            }
        )
    return jobs


def fetch_jobs(employer: "Employer") -> list[dict]:
    token = employer.board_token()
    url = f"https://{token}.recruitee.com/api/offers/"
    log.info("recruitee.fetch", company=employer.name, token=token)
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        payload = resp.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        log.warning("recruitee.http_error", company=employer.name, status=status, url=url)
        return []
    except (requests.RequestException, ValueError) as exc:
        log.warning("recruitee.request_error", company=employer.name, error=str(exc))
        return []
    jobs = parse_offers(payload if isinstance(payload, dict) else {}, employer.name)
    log.info("recruitee.done", company=employer.name, count=len(jobs))
    return jobs
