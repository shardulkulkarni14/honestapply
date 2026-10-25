"""Personio public job-feed fetcher (the ATS most German SMEs and start-ups use).

Public endpoint (no auth required):
    GET https://{token}.jobs.personio.de/xml            (German text)
    GET https://{token}.jobs.personio.de/xml?language=en

Employer options (``employers.yaml``):
    token    : the career-site sub-domain, e.g. ``acme`` for acme.jobs.personio.de
    host     : full host when the company uses a custom domain (e.g. ``jobs.acme.com``)
    language : ``en`` to request the English feed (default: the account's language)

Field mapping (Personio XML -> Job):
    id              -> external_id, and the posting URL https://{host}/job/{id}
    name            -> title
    office          -> location (+ additionalOffices when present)
    jobDescriptions -> description (every section, HTML stripped)
    createdAt       -> posted_at
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

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
    return host or f"{employer.board_token()}.jobs.personio.de"


def _text(node: ET.Element | None, tag: str) -> str:
    child = node.find(tag) if node is not None else None
    return (child.text or "").strip() if child is not None and child.text else ""


def parse_feed(xml_bytes: bytes | str, company: str, host: str) -> list[dict]:
    """Map a ``<workzag-jobs>`` feed to honestapply job dicts."""
    root = ET.fromstring(xml_bytes)
    jobs: list[dict] = []
    for pos in root.iter("position"):
        pid = _text(pos, "id")
        title = _text(pos, "name")
        if not pid or not title:
            continue
        office = _text(pos, "office")
        extra = _text(pos, "additionalOffices")
        location = ", ".join(p for p in (office, extra) if p)
        sections = []
        for section in pos.iter("jobDescription"):
            name = _text(section, "name")
            body = strip_html(_text(section, "value"))
            if body:
                sections.append(f"{name}: {body}" if name else body)
        jobs.append(
            {
                "company": company,
                "title": title,
                "location": location,
                "url": f"https://{host}/job/{pid}",
                "external_id": pid,
                "description": " ".join(sections) or None,
                "source_board": "personio",
                "_department": _text(pos, "department"),
                "_employment_type": _text(pos, "employmentType"),
                "_seniority": _text(pos, "seniority"),
                "_posted_at": _text(pos, "createdAt") or None,
            }
        )
    return jobs


def fetch_jobs(employer: "Employer") -> list[dict]:
    """Fetch every open position from a Personio career site."""
    host = feed_host(employer)
    url = f"https://{host}/xml"
    log.info("personio.fetch", company=employer.name, host=host)
    jobs = _fetch_feed(url, None, employer.name, host)
    if jobs is None:
        return []
    # A requested translation only carries text for jobs that were translated;
    # untranslated ones come back with empty sections. Merge it over the
    # account-language feed instead of replacing it.
    language = str(getattr(employer, "language", "") or "").strip()
    if language:
        localized = _fetch_feed(url, {"language": language}, employer.name, host) or []
        by_id = {j["external_id"]: j for j in localized if j.get("description")}
        for job in jobs:
            loc = by_id.get(job["external_id"])
            if loc:
                job["title"] = loc["title"] or job["title"]
                job["description"] = loc["description"]
    log.info("personio.done", company=employer.name, count=len(jobs))
    return jobs


def _fetch_feed(url: str, params: dict | None, company: str, host: str) -> list[dict] | None:
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        log.warning("personio.http_error", company=company, status=status, url=url)
        return None
    except requests.RequestException as exc:
        log.warning("personio.request_error", company=company, error=str(exc))
        return None
    try:
        return parse_feed(resp.content, company, host)
    except ET.ParseError as exc:
        log.warning("personio.bad_xml", company=company, error=str(exc))
        return None
