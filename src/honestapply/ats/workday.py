"""Workday ATS helper.

Quirks / known friction points:
- Multi-step wizard at *.myworkdayjobs.com. Each step ends with a "Next" button.
  data-automation-id attributes are the most stable selectors.
- Login is almost always required. The orchestrator should check for a persisted
  browser session at settings.browser_profile_dir before attempting to fill the form.
  If no session exists, mark needs_human so the user can log in manually first.
- File upload: click the "Upload" or "Add" button first, then target the hidden
  <input type="file" data-automation-id="..."> directly via Playwright.
- EEO / voluntary self-ID section is typically the last wizard step before the
  final review screen.
- CAPTCHAs appear occasionally on the login page; screenshot + needs_human is the
  correct response.
- Some Workday tenants use a custom domain (e.g. wd3.myworkdayjobs.com/companyname)
  rather than jobs.companyname.com. Detect via "workday" in the host.
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

_TIMEOUT = 20
_HEADERS = {
    "User-Agent": "honestapply/1.0 (+https://github.com/honestapply)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
_PAGE = 20  # Workday's cxs endpoint caps page size at 20
_MAX_POSTINGS = 600


def _job_url(host: str, site: str, external_path: str) -> str:
    site = (site or "").strip("/")
    path = external_path or ""
    if path and not path.startswith("/"):
        path = "/" + path
    return f"https://{host}/{site}{path}"


def fetch_jobs(employer: "Employer") -> list[dict]:
    """Fetch live postings from a company's public Workday career site.

    Requires employer fields (extra="allow"):
        host:   the Workday host, e.g. "acme.wd3.myworkdayjobs.com"
        site:   the career-site path segment, e.g. "External" or "AcmeCareers"
        tenant: (optional) the cxs tenant; defaults to host's first label.
    Optional: `q` (searchText keyword). Posts to the public cxs jobs endpoint
    and pages through results. Descriptions are backfilled by the enrich stage.
    """
    host = str(getattr(employer, "host", "") or "").strip()
    site = str(getattr(employer, "site", "") or "").strip()
    if not host or not site:
        log.warning("workday.misconfigured", company=employer.name,
                    detail="employer needs `host` and `site` fields")
        return []
    tenant = str(getattr(employer, "tenant", "") or "") or host.split(".")[0]
    keyword = str(getattr(employer, "q", "") or "")

    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    log.info("workday.fetch", company=employer.name, host=host, site=site, tenant=tenant)

    jobs: list[dict] = []
    offset = 0
    total = None
    while offset < _MAX_POSTINGS:
        body = {"appliedFacets": {}, "limit": _PAGE, "offset": offset, "searchText": keyword}
        try:
            resp = requests.post(endpoint, json=body, timeout=_TIMEOUT, headers=_HEADERS)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            log.warning("workday.http_error", company=employer.name,
                        status=exc.response.status_code if exc.response else None, url=endpoint)
            break
        except requests.RequestException as exc:
            log.warning("workday.request_error", company=employer.name, error=str(exc))
            break

        data = resp.json() if resp.content else {}
        if total is None:
            total = data.get("total")
            log.info("workday.total", company=employer.name, total=total)
        postings = data.get("jobPostings") or []
        if not postings:
            break

        for item in postings:
            external_path = item.get("externalPath") or ""
            if not external_path:
                continue
            jobs.append(
                {
                    "company": employer.name,
                    "title": (item.get("title") or "").strip(),
                    "location": (item.get("locationsText") or "").strip(),
                    "url": _job_url(host, site, external_path),
                    "external_id": str(item.get("bulletFields", [external_path])[0] if item.get("bulletFields") else external_path),
                    "description": "",  # backfilled by enrich
                    "source_board": "workday",
                }
            )

        offset += _PAGE
        if total is not None and offset >= total:
            break

    log.info("workday.done", company=employer.name, count=len(jobs))
    return jobs


_FALLBACK: dict = {
    "first_name": 'input[data-automation-id="legalNameSection_firstName"]',
    "last_name": 'input[data-automation-id="legalNameSection_lastName"]',
    "email": 'input[data-automation-id="email"]',
    "phone": 'input[data-automation-id="phone-number"]',
    "address_line1": 'input[data-automation-id="addressSection_addressLine1"]',
    "city": 'input[data-automation-id="addressSection_city"]',
    "state": 'input[data-automation-id="addressSection_countryRegion"]',
    "postal_code": 'input[data-automation-id="addressSection_postalCode"]',
    "country": 'select[data-automation-id="addressSection_country"]',
    "resume_upload": 'input[type="file"][data-automation-id*="Resume"]',
    "cover_letter_upload": 'input[type="file"][data-automation-id*="Cover"]',
    "how_did_you_hear": 'select[data-automation-id*="referral"], select[data-automation-id*="source"]',
    "submit": 'button[data-automation-id="bottom-navigation-next-button"], button[data-automation-id="click_done"]',
}


def field_hints() -> dict:
    """Return Workday selector hints from config, falling back to hardcoded values."""
    selectors = load_ats_selectors()
    return selectors.get("workday", _FALLBACK)
