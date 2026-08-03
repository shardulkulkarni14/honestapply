"""Stage 1 — Discover jobs from job boards (via python-jobspy) and company ATS pages.

Entry point:
    run_discover(sources: list[str] | None = None, filter_location: bool = True) -> int
        Returns the count of NEW jobs inserted into the database.

Sources:
    Job board names: indeed, linkedin, glassdoor, zip_recruiter, google
                     (note: the jobspy `google` board returns empty without an
                      API key — prefer `indeed` for keyword/company sweeps)
    ATS names:       greenhouse, lever, ashby, smartrecruiters, workday

The `sources` filter (e.g. ["greenhouse"] or ["indeed", "glassdoor"]) restricts
which sources are run. If None, all configured sources run.

When `filter_location=True` (default), jobs from company ATS boards whose location
clearly falls outside Germany/EU/Remote are dropped before insertion.  Jobs with
empty/unknown locations are always kept to avoid false-negative drops.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from honestapply.ats.detect import detect_ats
from honestapply.config import load_employers, load_profile, load_searches
from honestapply.db.models import Job, Status, url_hash, utcnow
from honestapply.db.session import init_db, session_scope
from honestapply.logging_setup import get_logger

log = get_logger(__name__)

# Mapping from board name -> jobspy site_name argument
_BOARD_TO_JOBSPY = {
    "indeed": "indeed",
    "linkedin": "linkedin",
    "glassdoor": "glassdoor",
    "zip_recruiter": "zip_recruiter",
    "ziprecruiter": "zip_recruiter",
    "google": "google",
}

# ATS fetcher modules (lazy-imported inside the function to keep startup fast)
_ATS_MODULES = {
    "greenhouse": "honestapply.ats.greenhouse",
    "lever": "honestapply.ats.lever",
    "ashby": "honestapply.ats.ashby",
    "smartrecruiters": "honestapply.ats.smartrecruiters",
    "workday": "honestapply.ats.workday",
}


# ---------------------------------------------------------------------------
# Location filter
# ---------------------------------------------------------------------------

# Keywords that indicate a preferred / acceptable region (case-insensitive).
# A job whose location contains ANY of these is considered a match.
_EU_KEEP_PATTERNS = re.compile(
    r"(\b("
    r"remote|deutschland|germany|munich|münchen|ingolstadt|berlin|hamburg|"
    r"frankfurt|cologne|köln|düsseldorf|duesseldorf|stuttgart|nuremberg|nürnberg|"
    r"augsburg|dortmund|essen|leipzig|hannover|hanover|"
    # German tech hubs beyond the top-10 cities. Without these, boards that
    # render a location as "Freiburg, BW, DE" (no country word) were dropped —
    # which silently discarded every posting from AI employers headquartered
    # outside the big cities.
    r"freiburg|heidelberg|mannheim|aachen|münster|muenster|erlangen|regensburg|"
    r"ulm|braunschweig|wolfsburg|jena|potsdam|kiel|darmstadt|kassel|bielefeld|"
    r"wiesbaden|bochum|würzburg|wuerzburg|osnabrück|paderborn|lübeck|mainz|"
    r"walldorf|karlsruhe|bremen|dresden|bonn|"
    r"europe|eu\b|emea|dach|"
    r"amsterdam|paris|vienna|wien|zurich|zürich|milan|barcelona|"
    r"stockholm|oslo|copenhagen|helsinki|warsaw|bratislava|prague|budapest"
    r")\b"
    # Trailing ISO country code, e.g. "Freiburg, BW, DE".
    r"|,\s*DE\s*$)",
    re.IGNORECASE,
)

# Keywords that indicate a clearly non-EU / non-remote location.
# A job whose location matches ONLY these (and nothing in _EU_KEEP_PATTERNS) is dropped.
_NON_EU_DROP_PATTERNS = re.compile(
    r"\b("
    r"san francisco|new york|nyc|los angeles|seattle|boston|chicago|austin|"
    r"toronto|vancouver|montreal|mexico|sao paulo|buenos aires|"
    r"london|uk\b|united kingdom|manchester|edinburgh|dublin(?!.*germany)(?!.*eu)|"
    r"bengaluru|bangalore|mumbai|hyderabad|chennai|pune|delhi|india|"
    r"singapore|tokyo|osaka|beijing|shanghai|hong kong|seoul|"
    r"sydney|melbourne|auckland|dubai|riyadh|tel aviv"
    r")\b",
    re.IGNORECASE,
)


def location_matches(job_location: str, preferred: list[str]) -> bool:
    """Return True if *job_location* is acceptable given the user's preferred locations.

    Logic:
    1. Empty / None location  → keep (don't drop unknowns).
    2. Matches a preferred city (from the profile) → keep.
    3. Matches a general EU/EMEA/Germany/remote keyword → keep.
    4. Matches a clearly-non-EU city/country AND nothing in rule 2 or 3 → drop.
    5. Anything else → keep (err on the side of inclusion).
    """
    if not job_location:
        return True

    loc_lower = job_location.lower()

    # Rule 2 — preferred cities from profile (exact substring, case-insensitive)
    for pref in preferred:
        if pref.lower() in loc_lower:
            return True

    # Rule 3 — general EU / remote keyword
    if _EU_KEEP_PATTERNS.search(job_location):
        return True

    # Rule 4 — clearly non-EU and nothing above matched → drop
    if _NON_EU_DROP_PATTERNS.search(job_location):
        return False

    # Rule 5 — unknown / ambiguous → keep
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _already_exists(session: Any, uh: str, company: str, title: str) -> bool:
    """Return True if a job with this url_hash OR (company+title+url_hash) already exists."""
    from sqlalchemy import select
    existing = session.execute(
        select(Job.id).where(Job.url_hash == uh).limit(1)
    ).scalar_one_or_none()
    return existing is not None


def _safe_parse_date(value: Any) -> datetime | None:
    """Best-effort date parsing from various formats returned by jobspy / ATS APIs."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if hasattr(value, "isoformat"):  # date object
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value[:len(fmt)], fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                continue
        # ISO with offset like "2026-05-22T16:05:41-04:00"
        try:
            import email.utils
            ts = email.utils.parsedate_to_datetime(value)
            return ts
        except Exception:
            pass
        # isoformat with +00:00 suffix handled by datetime.fromisoformat on Py3.11+
        try:
            from datetime import datetime as dt_cls
            d = dt_cls.fromisoformat(value)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d
        except Exception:
            pass
    return None


def _insert_job(session: Any, data: dict) -> bool:
    """Insert a single job dict; return True if inserted, False if duplicate/error."""
    uh = url_hash(data.get("url", ""))
    company = (data.get("company") or "").strip()
    title = (data.get("title") or "").strip()

    if not uh or not company or not title:
        return False

    if _already_exists(session, uh, company, title):
        return False

    posted_at = _safe_parse_date(data.get("posted_at") or data.get("_posted_at") or data.get("_first_published") or data.get("_updated_at") or data.get("_published_at"))

    job = Job(
        source_board=data.get("source_board", ""),
        external_id=str(data.get("external_id", "")),
        url=data.get("url", ""),
        url_hash=uh,
        company=company,
        title=title,
        location=data.get("location", ""),
        description=data.get("description") or None,
        salary_range_text=data.get("salary_range_text") or None,
        posted_at=posted_at,
        discovered_at=utcnow(),
        status=Status.DISCOVERED,
        ats_type=detect_ats(data.get("url", "")),
    )
    try:
        session.add(job)
        session.flush()  # flush to catch IntegrityError before commit
        return True
    except IntegrityError:
        session.rollback()
        return False


# ---------------------------------------------------------------------------
# jobspy board scraping
# ---------------------------------------------------------------------------

def _clean_company(value: Any) -> str:
    """Normalise a jobspy company cell, rejecting pandas' NaN placeholder.

    A missing company arrives as the float NaN, and `str(nan)` is the literal
    "nan". 127 such rows reached the database, where the name would render into
    a cover letter greeting and an application form's employer field.
    """
    text = str(value or "").strip()
    if text.lower() in {"nan", "none", "null", "n/a", "-"}:
        return ""
    return text


def _clean_url(value: Any) -> str:
    """Normalise a jobspy URL cell (may be NaN / None / float) to a plain string."""
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _best_url(row: Any) -> str:
    """Pick the most *appliable* URL jobspy gives us for a listing.

    jobspy returns two: ``job_url`` (the board's own listing page) and
    ``job_url_direct`` (the employer's real apply link — often an Ashby /
    Greenhouse / Workday posting). This previously preferred ``job_url``, so
    Indeed and LinkedIn listings were stored under those hosts and then dropped
    by `prefilter` as dead hosts (Indeed listings expire fast; LinkedIn apply is
    ban-guarded). That discarded thousands of jobs whose direct link was
    perfectly appliable. Prefer the direct link whenever the board exposes one
    and it is not itself a dead host.
    """
    direct = _clean_url(row.get("job_url_direct"))
    listing = _clean_url(row.get("job_url"))
    if direct and not any(m in direct.lower() for m in ("indeed.", "linkedin.")):
        return direct
    return listing or direct


def _run_jobspy_search(entry: Any) -> list[dict]:
    """Run a single SearchEntry via python-jobspy. Returns a list of raw dicts."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.warning("discover.jobspy_not_installed")
        return []

    site = _BOARD_TO_JOBSPY.get(entry.board.lower())
    if site is None:
        log.warning("discover.unknown_board", board=entry.board)
        return []

    kwargs: dict[str, Any] = {
        "site_name": site,
        "search_term": entry.query,
        "location": entry.location or None,
        "results_wanted": entry.max_results,
        "country_indeed": entry.country or "Germany",
        "is_remote": entry.is_remote,
        "verbose": 0,
    }
    if entry.hours_old:
        kwargs["hours_old"] = entry.hours_old

    log.info("discover.jobspy", board=site, query=entry.query, location=entry.location)
    try:
        df = scrape_jobs(**kwargs)
    except Exception as exc:
        log.warning("discover.jobspy_error", board=site, query=entry.query, error=str(exc))
        return []

    if df is None or df.empty:
        log.info("discover.jobspy_empty", board=site, query=entry.query)
        return []

    results: list[dict] = []
    for _, row in df.iterrows():
        # Build salary text from structured fields if present
        salary_text = ""
        try:
            if row.get("min_amount") or row.get("max_amount"):
                lo = row.get("min_amount")
                hi = row.get("max_amount")
                curr = row.get("currency") or ""
                interval = row.get("interval") or ""
                parts = []
                if lo:
                    parts.append(f"{curr}{int(lo):,}")
                if hi:
                    parts.append(f"{curr}{int(hi):,}")
                salary_text = "–".join(parts)
                if interval:
                    salary_text += f" / {interval}"
        except Exception:
            pass

        results.append(
            {
                "source_board": site,
                "external_id": str(row.get("id") or ""),
                "url": _best_url(row),
                # pandas renders a missing company as the float NaN, whose str()
                # is the literal "nan" — which then reaches cover letters as
                # "Dear nan" and application forms as the employer name.
                "company": _clean_company(row.get("company")),
                "title": str(row.get("title") or ""),
                "location": str(row.get("location") or ""),
                "description": row.get("description") or None,
                "salary_range_text": salary_text or None,
                "posted_at": row.get("date_posted"),
            }
        )
    return results


# ---------------------------------------------------------------------------
# ATS company board fetching
# ---------------------------------------------------------------------------

def _run_ats_fetch(employer: Any) -> list[dict]:
    """Fetch jobs for one employer from its ATS board."""
    import importlib

    mod_path = _ATS_MODULES.get(employer.ats.lower())
    if mod_path is None:
        log.warning("discover.unknown_ats", employer=employer.name, ats=employer.ats)
        return []

    try:
        mod = importlib.import_module(mod_path)
        return mod.fetch_jobs(employer)
    except Exception as exc:
        log.warning("discover.ats_error", employer=employer.name, ats=employer.ats, error=str(exc))
        return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_discover(sources: list[str] | None = None, filter_location: bool = True) -> int:
    """Discover jobs. Returns count of NEW jobs inserted.

    Args:
        sources: restrict which sources to run (e.g. ["greenhouse"]).  None = all.
        filter_location: when True (default), drop ATS jobs whose location is
            clearly outside Germany / EU / Remote before inserting them.
    """
    init_db()

    sources_lower = [s.lower() for s in sources] if sources else None

    def _want(source: str) -> bool:
        if sources_lower is None:
            return True
        return source.lower() in sources_lower

    # Load preferred locations from the user's profile (used for location filtering)
    preferred_locations: list[str] = []
    if filter_location:
        try:
            profile = load_profile()
            preferred_locations = profile.preferred_locations or []
        except Exception:
            preferred_locations = []

    inserted_total = 0

    # ---- 1. jobspy boards ----
    searches = load_searches()
    log.info("discover.searches_loaded", count=len(searches))

    for entry in searches:
        if not _want(entry.board):
            continue
        raw_jobs = _run_jobspy_search(entry)
        batch_inserted = 0
        with session_scope() as session:
            for job_data in raw_jobs:
                if _insert_job(session, job_data):
                    batch_inserted += 1
        log.info("discover.search_done", board=entry.board, query=entry.query, inserted=batch_inserted)
        inserted_total += batch_inserted

    # ---- 2. ATS company boards ----
    employers = load_employers()
    log.info("discover.employers_loaded", count=len(employers))

    for employer in employers:
        if not _want(employer.ats):
            continue
        raw_jobs = _run_ats_fetch(employer)

        # Apply location filter to company-board fetches
        if filter_location:
            kept: list[dict] = []
            dropped_count = 0
            for job_data in raw_jobs:
                job_loc = job_data.get("location") or ""
                if location_matches(job_loc, preferred_locations):
                    kept.append(job_data)
                else:
                    dropped_count += 1
            if dropped_count:
                log.info(
                    "discover.location_filter",
                    employer=employer.name,
                    fetched=len(raw_jobs),
                    kept=len(kept),
                    dropped=dropped_count,
                )
            raw_jobs = kept

        batch_inserted = 0
        with session_scope() as session:
            for job_data in raw_jobs:
                if _insert_job(session, job_data):
                    batch_inserted += 1
        log.info(
            "discover.employer_done",
            employer=employer.name,
            ats=employer.ats,
            inserted=batch_inserted,
            fetched=len(raw_jobs),
        )
        inserted_total += batch_inserted

    log.info("discover.complete", total_inserted=inserted_total)
    return inserted_total
