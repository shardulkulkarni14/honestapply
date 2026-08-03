"""Stage 2 — Enrich discovered jobs with full descriptions.

Entry point:
    run_enrich(limit: int | None = None) -> int
        Returns count of jobs successfully enriched.

3-tier extraction cascade per job:
    (a) JSON-LD  — parse <script type="application/ld+json"> blocks for JobPosting
    (b) ATS CSS selectors — BeautifulSoup selectors keyed by ATS type
    (c) LLM fallback — truncated HTML fed to get_provider().complete()

If a job already has a non-empty description (e.g., from the Greenhouse board
API which returns HTML content), the network fetch is skipped and the stored
HTML is cleaned/parsed in-place.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from honestapply.ats.detect import detect_ats
from honestapply.config import get_settings
from honestapply.db.models import Job, Status, utcnow
from honestapply.db.session import init_db, session_scope
from honestapply.logging_setup import get_logger

log = get_logger(__name__)

_FETCH_TIMEOUT = 20
_MAX_HTML_FOR_LLM = 15_000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Remove all HTML tags; decode common entities; collapse whitespace."""
    if not html:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    for ent, ch in [
        ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " "),
        ("&#8211;", "–"), ("&#8212;", "—"), ("&#8216;", "'"), ("&#8217;", "'"),
    ]:
        text = text.replace(ent, ch)
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Tier A — JSON-LD extraction
# ---------------------------------------------------------------------------

def _extract_jsonld(html: str) -> dict:
    """Parse <script type="application/ld+json"> blocks and find a JobPosting."""
    result: dict[str, Any] = {}
    for raw_json in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(raw_json.strip())
        except json.JSONDecodeError:
            continue

        # Handle @graph arrays
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data:
            items = data["@graph"]

        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "JobPosting":
                continue

            # description
            desc = item.get("description", "")
            if desc:
                result["description"] = _strip_html(str(desc))

            # title
            t = item.get("title") or item.get("name")
            if t:
                result["title"] = str(t).strip()

            # location
            loc = item.get("jobLocation")
            if loc:
                if isinstance(loc, list):
                    loc = loc[0]
                if isinstance(loc, dict):
                    addr = loc.get("address") or {}
                    if isinstance(addr, dict):
                        parts = [
                            addr.get("streetAddress", ""),
                            addr.get("addressLocality", ""),
                            addr.get("addressRegion", ""),
                            addr.get("addressCountry", ""),
                        ]
                        result["location"] = ", ".join(p for p in parts if p).strip(", ")
                    elif isinstance(addr, str):
                        result["location"] = addr
                elif isinstance(loc, str):
                    result["location"] = loc

            # salary
            salary = item.get("baseSalary") or item.get("estimatedSalary")
            if salary:
                if isinstance(salary, dict):
                    val = salary.get("value") or {}
                    if isinstance(val, dict):
                        lo = val.get("minValue")
                        hi = val.get("maxValue")
                        currency = salary.get("currency", "")
                        unit_text = val.get("unitText", "")
                        if lo or hi:
                            parts = []
                            if lo:
                                parts.append(f"{currency}{int(lo):,}")
                            if hi:
                                parts.append(f"{currency}{int(hi):,}")
                            result["salary_range_text"] = "–".join(parts) + (f" /{unit_text}" if unit_text else "")
                    elif isinstance(val, (int, float)):
                        currency = salary.get("currency", "")
                        result["salary_range_text"] = f"{currency}{int(val):,}"
                else:
                    result["salary_range_text"] = str(salary)

            # datePosted
            dp = item.get("datePosted")
            if dp:
                result["_date_posted"] = dp

            if result:
                return result  # use the first matching block
    return result


# ---------------------------------------------------------------------------
# Tier B — ATS-specific CSS selectors via BeautifulSoup
# ---------------------------------------------------------------------------

def _parse_with_bs4(html: str, ats_type: str) -> dict:
    """Use BeautifulSoup CSS selectors keyed by ATS type."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, Any] = {}

    selectors = {
        "greenhouse": [
            "#content",
            ".job__description",
            ".content-intro",
            ".job-description",
        ],
        "lever": [
            ".section[data-qa='job-description']",
            ".section-wrapper .section",
            ".posting-page",
            ".content",
        ],
        "ashby": [
            ".ashby-job-posting-brief-description",
            ".job-posting-description",
            "[class*='description']",
            "main",
        ],
        "workday": [
            "[data-automation-id='job-description']",
            ".job-description",
        ],
        "smartrecruiters": [
            ".job-description",
            "#st-overview",
        ],
        "generic": [
            ".job-description",
            ".job-details",
            "article",
            "main",
        ],
    }

    ats_key = ats_type if ats_type in selectors else "generic"
    for sel in selectors[ats_key]:
        el = soup.select_one(sel)
        if el:
            result["description"] = _strip_html(str(el))
            break

    # Try to find salary in structured data or common selectors
    if not result.get("salary_range_text"):
        for sel in [".salary", ".compensation", "[class*='salary']", "[class*='compensation']"]:
            el = soup.select_one(sel)
            if el:
                salary_text = _strip_html(str(el)).strip()
                if salary_text and len(salary_text) < 200:
                    result["salary_range_text"] = salary_text
                    break

    return result


# ---------------------------------------------------------------------------
# Tier C — LLM fallback
# ---------------------------------------------------------------------------

def _extract_with_llm(html: str, job: Any) -> dict:
    """Ask the configured LLM to extract job details from HTML."""
    try:
        from honestapply.llm.base import get_provider
        provider = get_provider()
    except Exception as exc:
        log.warning("enrich.llm_unavailable", error=str(exc))
        return {}

    truncated = html[:_MAX_HTML_FOR_LLM]
    prompt = (
        "You are a job data extractor. Given the following HTML of a job posting page, "
        "extract and return ONLY these fields as plain text (no HTML):\n\n"
        "JOB DESCRIPTION: <the full job description>\n"
        "REQUIREMENTS: <required skills and qualifications>\n"
        "LOCATION: <office location or 'Remote'>\n"
        "SALARY: <salary or compensation range, or 'Not specified'>\n\n"
        f"HTML:\n{truncated}\n\n"
        "Output each field on a new line starting with the label."
    )

    try:
        raw = provider.complete(prompt)
    except Exception as exc:
        log.warning("enrich.llm_error", error=str(exc), job_id=getattr(job, "id", None))
        return {}

    result: dict[str, Any] = {}
    # Parse labeled sections
    desc_match = re.search(r"JOB DESCRIPTION:\s*(.*?)(?=REQUIREMENTS:|LOCATION:|SALARY:|$)", raw, re.DOTALL | re.IGNORECASE)
    req_match = re.search(r"REQUIREMENTS:\s*(.*?)(?=JOB DESCRIPTION:|LOCATION:|SALARY:|$)", raw, re.DOTALL | re.IGNORECASE)
    loc_match = re.search(r"LOCATION:\s*(.*?)(?=JOB DESCRIPTION:|REQUIREMENTS:|SALARY:|$)", raw, re.DOTALL | re.IGNORECASE)
    sal_match = re.search(r"SALARY:\s*(.*?)(?=JOB DESCRIPTION:|REQUIREMENTS:|LOCATION:|$)", raw, re.DOTALL | re.IGNORECASE)

    if desc_match:
        result["description"] = desc_match.group(1).strip()
    if req_match:
        result["requirements"] = req_match.group(1).strip()
    if loc_match:
        loc_text = loc_match.group(1).strip()
        if loc_text and loc_text.lower() not in ("not specified", "n/a", ""):
            result["location"] = loc_text
    if sal_match:
        sal_text = sal_match.group(1).strip()
        if sal_text and sal_text.lower() not in ("not specified", "n/a", ""):
            result["salary_range_text"] = sal_text

    # If the whole response is just a single blob (stub provider), store as description
    if not result and raw and raw != "[stub] response":
        result["description"] = raw.strip()

    return result


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _safe_parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        from datetime import datetime as dt_cls
        d = dt_cls.fromisoformat(value.strip())
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        pass
    # Try YYYY-MM-DD
    m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return d
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Network fetch
# ---------------------------------------------------------------------------

def _fetch_html(url: str) -> str | None:
    """Fetch a URL and return its HTML content, or None on error."""
    try:
        import requests
        resp = requests.get(url, timeout=_FETCH_TIMEOUT, headers=_HEADERS, allow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        log.warning("enrich.fetch_error", url=url, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Per-job enrichment
# ---------------------------------------------------------------------------

def _enrich_job(job: Any) -> bool:
    """Enrich a single Job. Returns True if successfully enriched."""
    log.info("enrich.job_start", job_id=job.id, company=job.company, title=job.title)

    ats_type = job.ats_type or detect_ats(job.url)
    html: str | None = None

    # If the job already has a description (e.g., from Greenhouse content=true),
    # use it — no network fetch needed.
    if job.description and len(job.description.strip()) > 50:
        # Already have content; clean it and skip fetch
        html = job.description  # treat as "html" for JSON-LD + BS4 passes
        skip_fetch = True
    else:
        skip_fetch = False
        if job.url:
            html = _fetch_html(job.url)

    if not html:
        log.warning("enrich.no_html", job_id=job.id)
        # Still mark enriched with what we have (avoids re-processing permanently broken URLs)
        return False

    merged: dict[str, Any] = {}

    # Tier A — JSON-LD (only worth trying if we fetched the live page)
    if not skip_fetch:
        jsonld = _extract_jsonld(html)
        merged.update(jsonld)

    # Tier B — ATS selectors
    if not merged.get("description"):
        bs4_data = _parse_with_bs4(html, ats_type)
        for k, v in bs4_data.items():
            if not merged.get(k):
                merged[k] = v

    # Tier C — LLM fallback if no description yet
    if not merged.get("description"):
        llm_data = _extract_with_llm(html, job)
        for k, v in llm_data.items():
            if not merged.get(k):
                merged[k] = v

    # If we used the stored content and it passed through as-is, strip HTML
    if skip_fetch:
        merged["description"] = _strip_html(html)

    # Apply extracted data back to the job
    if merged.get("description"):
        job.description = merged["description"]
    if merged.get("requirements"):
        job.requirements = merged["requirements"]
    if merged.get("location") and not job.location:
        job.location = merged["location"]
    if merged.get("salary_range_text") and not job.salary_range_text:
        job.salary_range_text = merged["salary_range_text"]
    if merged.get("_date_posted") and not job.posted_at:
        job.posted_at = _safe_parse_date(merged["_date_posted"])

    job.status = Status.ENRICHED
    log.info("enrich.job_done", job_id=job.id, description_len=len(job.description or ""))
    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_enrich(limit: int | None = None, ids: list[int] | None = None) -> int:
    """Enrich discovered jobs. Returns count enriched.

    When *ids* is given, only those job IDs are processed (still restricted to
    DISCOVERED), so a curated subset can be driven without draining the backlog.
    """
    init_db()

    with session_scope() as session:
        q = select(Job).where(Job.status == Status.DISCOVERED).order_by(Job.discovered_at)
        if ids:
            q = q.where(Job.id.in_(ids))
        if limit:
            q = q.limit(limit)
        jobs = session.execute(q).scalars().all()

    log.info("enrich.start", count=len(jobs))

    enriched_count = 0
    for job in jobs:
        try:
            with session_scope() as session:
                # Re-attach the job to this session
                j = session.get(Job, job.id)
                if j is None or j.status != Status.DISCOVERED:
                    continue
                success = _enrich_job(j)
                if success:
                    enriched_count += 1
                else:
                    # Network error or empty content — skip but leave as DISCOVERED
                    pass
        except Exception as exc:
            log.warning("enrich.job_error", job_id=job.id, error=str(exc))
            continue

    log.info("enrich.complete", enriched=enriched_count)
    return enriched_count
