"""SmartRecruiters ATS helper + public-board discovery fetcher.

Discovery (no auth): the public Posting API exposes every company's live jobs at
    GET https://api.smartrecruiters.com/v1/companies/{companyId}/postings
        ?limit=100&offset=N[&country=de][&q=keyword]
This is how big German employers (Bosch, Continental, …) become reachable — they
use SmartRecruiters rather than Greenhouse/Lever/Ashby. `fetch_jobs(employer)`
pages through the list and returns dicts compatible with the discover stage.
Descriptions are intentionally left empty here; the enrich stage backfills them
from the public posting URL.

Quirks / known friction points (apply side):
- Application form lives at:
    careers.smartrecruiters.com/{company}/job/{job-id}/apply
- Multi-step flow: Profile -> Questions -> Review. Navigate via a "Continue" button
  (data-hook="continue") between steps.
- File upload uses a custom drag-and-drop widget. Trigger the underlying
  <input type="file"> directly via Playwright rather than clicking the visual button.
- Step 2 (Questions) contains screening questions rendered as radio buttons,
  checkboxes, or free-text fields; use the answers YAML to fill these.
- Some tenants require a SmartRecruiters account (login) before applying;
  check for a persisted session in settings.browser_profile_dir first.
- The "Review" step (step 3) shows a summary before final submission — always
  take the pre_submit screenshot here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import requests

from honestapply.config import load_ats_selectors
from honestapply.logging_setup import get_logger

if TYPE_CHECKING:
    from honestapply.config import Employer

log = get_logger(__name__)

_API = "https://api.smartrecruiters.com/v1/companies"
_TIMEOUT = 20
_HEADERS = {"User-Agent": "honestapply/1.0 (+https://github.com/honestapply)"}
_PAGE = 100
# Hard ceiling on postings pulled per employer (big tenants have thousands;
# the discover location filter + scoring gate trim further downstream).
_MAX_POSTINGS = 600


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _location_text(loc: dict) -> str:
    if not isinstance(loc, dict):
        return ""
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    text = ", ".join(p for p in parts if p)
    if loc.get("remote"):
        text = (text + ", Remote").lstrip(", ") if text else "Remote"
    return text


def fetch_jobs(employer: "Employer") -> list[dict]:
    """Fetch live postings from a company's public SmartRecruiters board.

    Reads optional employer fields (extra="allow"): `country` (ISO code, default
    "de" to bias toward German roles; set to "" for no country facet) and `q`
    (keyword filter). Returns dicts with company/title/location/url/external_id/
    source_board; description is left empty for the enrich stage to backfill.
    """
    company = employer.board_token()
    country = getattr(employer, "country", None)
    country = "de" if country is None else str(country)
    keyword = str(getattr(employer, "q", "") or "")

    base_params: dict = {"limit": _PAGE}
    if country:
        base_params["country"] = country
    if keyword:
        base_params["q"] = keyword

    url = f"{_API}/{company}/postings"
    jobs: list[dict] = []
    offset = 0
    total = None
    while offset < _MAX_POSTINGS:
        params = dict(base_params, offset=offset)
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT, headers=_HEADERS)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            log.warning("smartrecruiters.http_error", company=employer.name,
                        status=exc.response.status_code if exc.response else None, url=url)
            break
        except requests.RequestException as exc:
            log.warning("smartrecruiters.request_error", company=employer.name, error=str(exc))
            break

        data = resp.json() if resp.content else {}
        if total is None:
            total = data.get("totalFound")
            log.info("smartrecruiters.fetch", company=employer.name, token=company,
                     country=country or "(any)", total=total)
        content = data.get("content") or []
        if not content:
            break

        for item in content:
            posting_id = str(item.get("id") or item.get("uuid") or "")
            if not posting_id:
                continue
            jobs.append(
                {
                    "company": (item.get("company") or {}).get("name") or employer.name,
                    "title": (item.get("name") or "").strip(),
                    "location": _location_text(item.get("location") or {}),
                    "url": f"https://jobs.smartrecruiters.com/{company}/{posting_id}",
                    "external_id": posting_id,
                    "description": "",  # backfilled by enrich
                    "source_board": "smartrecruiters",
                    "_posted_at": _parse_date(item.get("releasedDate")),
                }
            )

        offset += _PAGE
        if total is not None and offset >= total:
            break

    log.info("smartrecruiters.done", company=employer.name, count=len(jobs))
    return jobs


_FALLBACK: dict = {
    "first_name": 'input[name="firstName"], input[id="firstName"]',
    "last_name": 'input[name="lastName"], input[id="lastName"]',
    "email": 'input[name="email"], input[type="email"]',
    "phone": 'input[name="phoneNumber"], input[id="phone"]',
    "location": 'input[name="location.city"], input[id*="location"]',
    "resume_upload": 'input[type="file"][accept*="pdf"], input[type="file"][name*="resume"]',
    "cover_letter_upload": 'input[type="file"][name*="cover"]',
    "linkedin_url": 'input[name*="linkedin"], input[placeholder*="linkedin"]',
    "website_url": 'input[name*="website"], input[placeholder*="website"]',
    "submit": 'button[data-hook="continue"], button.wds-btn--primary[type="submit"]',
}


def field_hints() -> dict:
    """Return SmartRecruiters selector hints from config, falling back to hardcoded values."""
    selectors = load_ats_selectors()
    return selectors.get("smartrecruiters", _FALLBACK)
