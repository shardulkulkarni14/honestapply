"""Discovery source: the Bundesagentur für Arbeit *Jobbörse* — Germany's largest
public job database.

This talks to the agency's own mobile-app JSON endpoint
(``rest.arbeitsagentur.de/.../pc/v6/jobs``) with the documented public app key,
so there is **no scraping and no anti-bot circumvention** — unlike StepStone,
which we deliberately do not support.

Posture (important): the Jobbörse data is **not** open data. The agency holds
copyright and limits use to *job-placement purposes*. honestapply uses it only
to find roles for the single user running it, and **never redistributes the
listings** — they live only in the local DB (gitignored), exactly like every
other source. Keep it that way: do not publish, export, or bulk-share results.

A search entry opts in with ``board: arbeitsagentur`` in ``searches.yaml``.
Descriptions are not in the list response; the normal ``enrich`` stage fetches
each full posting from its URL afterwards.

**Source, not apply channel.** When a listing carries no ``externeURL`` the only
URL is the Jobbörse detail page, whose employer contact/apply link sits behind
an image CAPTCHA. :func:`attach_employer_links` swaps such URLs for the
employer's own posting when a public ATS board carries the same title (see
``honestapply.ats.resolve``); the rest are dropped by ``prefilter`` and refused
by ``apply`` (``HONESTAPPLY_ARBEITSAGENTUR_APPLY`` opts back in).
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import quote

import requests

from honestapply.logging_setup import get_logger

log = get_logger(__name__)

_BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
# The public application key documented by the Bundesagentur's own iOS app and
# the community bundesAPI project. It is not a secret and grants read-only search.
_API_KEY = "jobboerse-jobsuche"
_USER_AGENT = (
    "Jobsuche/2.9.2 (de.arbeitsagentur.jobboerse; build:1077; iOS 15.1.0) "
    "Alamofire/5.4.4"
)
_HEADERS = {"X-API-Key": _API_KEY, "User-Agent": _USER_AGENT, "Accept": "application/json"}

_PAGE_SIZE = 100          # the API's max page size
_MAX_PAGES = 25           # hard safety ceiling regardless of max_results
_TIMEOUT = 20             # seconds per request
# verguetungsangabe / vertragsdauer sentinels that mean "nothing to show".
_EMPTY_SALARY = {"", "KEINE_ANGABE", "KEINE_ANGABEN"}


def _format_location(item: dict) -> str:
    """A pipeline-friendly location string from the first work location.

    The Germany geo filter keys off a trailing country word, so we emit
    ``"<city>, Germany"`` (the API's ``land`` is ``DEUTSCHLAND``). Falls back to
    a bare ``"Germany"`` when no city is given.
    """
    locs = item.get("stellenlokationen") or []
    if locs:
        addr = (locs[0] or {}).get("adresse") or {}
        city = (addr.get("ort") or "").strip()
        if city:
            return f"{city}, Germany"
    if item.get("homeofficemoeglich"):
        return "Remote (Germany)"
    return "Germany"


def _job_url(item: dict, refnr: str) -> str:
    """Most appliable URL: the employer's external link when present, else the
    canonical Arbeitsagentur detail page for the reference number."""
    ext = (item.get("externeURL") or "").strip()
    if ext:
        return ext
    if refnr:
        return f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{quote(refnr, safe='')}"
    return ""


def parse_item(item: dict) -> dict | None:
    """Map one ``ergebnisliste`` entry to honestapply's job dict.

    Returns ``None`` when the entry lacks the minimum (title, employer, url) that
    ``_insert_job`` requires, so callers can simply drop falsy results.
    """
    if not isinstance(item, dict):
        return None
    title = (item.get("stellenangebotsTitel") or "").strip()
    company = (item.get("firma") or "").strip()
    refnr = str(item.get("referenznummer") or "").strip()
    url = _job_url(item, refnr)
    if not title or not company or not url:
        return None

    salary = (item.get("verguetungsangabe") or "").strip()
    return {
        "source_board": "arbeitsagentur",
        "external_id": refnr,
        "url": url,
        "company": company,
        "title": title,
        "location": _format_location(item),
        "description": None,  # enrich stage fetches the full posting later
        "salary_range_text": salary if salary not in _EMPTY_SALARY else None,
        "posted_at": item.get("datumErsteVeroeffentlichung"),
    }


def run_arbeitsagentur_search(entry: Any) -> list[dict]:
    """Run one ``SearchEntry`` (``board: arbeitsagentur``) against the Jobbörse.

    Paginates up to ``entry.max_results``, de-duplicating by reference number
    within the run. Network/parse failures are logged and yield a partial (or
    empty) list rather than raising, matching the jobspy source's behaviour.
    """
    want = max(1, int(getattr(entry, "max_results", 25) or 25))
    pages = min(_MAX_PAGES, math.ceil(want / _PAGE_SIZE))

    base_params: dict[str, Any] = {
        "was": entry.query,
        "wo": getattr(entry, "location", "") or "",
        "size": str(_PAGE_SIZE),
        "umkreis": "25",      # km radius around the location
        "angebotsart": "1",   # 1 = Arbeit (employment), not training/self-employment
        "pav": "false",       # exclude private placement agencies
    }
    hours_old = getattr(entry, "hours_old", None)
    if hours_old:
        base_params["veroeffentlichtseit"] = str(max(1, round(hours_old / 24)))

    log.info("discover.arbeitsagentur", query=entry.query, location=entry.location, want=want)

    results: list[dict] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        params = dict(base_params, page=str(page))
        try:
            resp = requests.get(_BASE_URL, headers=_HEADERS, params=params, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            log.warning("discover.arbeitsagentur_error", query=entry.query, page=page, error=str(exc))
            break
        if resp.status_code != 200:
            log.warning("discover.arbeitsagentur_http", query=entry.query, page=page, status=resp.status_code)
            break
        try:
            payload = resp.json()
        except ValueError:
            log.warning("discover.arbeitsagentur_badjson", query=entry.query, page=page)
            break

        batch = payload.get("ergebnisliste") or []
        if not batch:
            break
        for item in batch:
            parsed = parse_item(item)
            if not parsed:
                continue
            key = parsed["external_id"] or parsed["url"]
            if key in seen:
                continue
            seen.add(key)
            results.append(parsed)
            if len(results) >= want:
                return results
        if len(batch) < _PAGE_SIZE:
            break  # last page

    return results


def attach_employer_links(results: list[dict], resolver: Any, skip_ids: set[str] | frozenset[str] = frozenset()) -> int:
    """Replace Jobbörse detail-page URLs with the employer's own posting.

    ``resolver`` is a :class:`honestapply.ats.resolve.BoardResolver`. Listings
    whose reference number is in ``skip_ids`` (already in the DB) are left
    alone so repeat discovery runs do not re-probe known jobs. Returns the
    number of listings relinked. Never raises: a resolver failure leaves the
    listing URL in place.
    """
    from honestapply.ats.detect import ARBEITSAGENTUR, detect_ats

    relinked = 0
    for job in results:
        if job.get("external_id") in skip_ids:
            continue
        if detect_ats(job.get("url") or "") != ARBEITSAGENTUR:
            continue
        try:
            hit = resolver.resolve(job.get("company") or "", job.get("title") or "")
        except Exception as exc:  # network / parse noise must not break discovery
            log.warning("discover.arbeitsagentur_resolve_error", company=job.get("company"), error=str(exc))
            continue
        if not hit:
            continue
        job["url"] = hit["url"]
        job["_resolved_board"] = hit["ats"]
        relinked += 1
        log.info(
            "discover.arbeitsagentur_relinked",
            company=job.get("company"),
            title=job.get("title"),
            ats=hit["ats"],
            similarity=hit.get("similarity"),
        )
    return relinked
