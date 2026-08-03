"""Smoke tests for discovery: ATS detection, board parsing, dedup constraint."""

from __future__ import annotations

import pytest


def test_detect_ats():
    from honestapply.ats.detect import detect_ats

    assert detect_ats("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert detect_ats("https://jobs.lever.co/acme/abc") == "lever"
    assert detect_ats("https://jobs.ashbyhq.com/acme/xyz") == "ashby"
    assert detect_ats("https://acme.wd1.myworkdayjobs.com/x") == "workday"
    assert detect_ats("https://jobs.smartrecruiters.com/acme/123") == "smartrecruiters"
    assert detect_ats("https://www.linkedin.com/jobs/view/123") == "linkedin"
    assert detect_ats("https://careers.acme.com/job/1") == "generic"


def test_greenhouse_parse_mocked(monkeypatch):
    """fetch_jobs should parse a Greenhouse-shaped payload without network."""
    from honestapply.ats import greenhouse
    from honestapply.config import Employer

    sample = {
        "jobs": [
            {
                "id": 42,
                "title": "Senior AI Engineer",
                "location": {"name": "Munich, Germany"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                "content": "<p>Python, LLM, RAG, FastAPI</p>",
                "updated_at": "2026-01-02T00:00:00Z",
            }
        ]
    }

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return sample

    monkeypatch.setattr(greenhouse.requests, "get", lambda *a, **k: _Resp())

    jobs = greenhouse.fetch_jobs(Employer(name="Acme", ats="greenhouse", token="acme"))
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "Senior AI Engineer"
    assert "greenhouse.io/acme/jobs/42" in j["url"]
    assert j.get("company")


def test_smartrecruiters_parse_mocked(monkeypatch):
    """SmartRecruiters fetch_jobs should parse the postings payload without network."""
    from honestapply.ats import smartrecruiters as sr
    from honestapply.config import Employer

    sample = {
        "totalFound": 1,
        "content": [
            {
                "id": "744000133914723",
                "name": "Senior AI Engineer",
                "company": {"name": "Bosch"},
                "location": {"city": "Munich", "region": "BY", "country": "de", "remote": False},
                "releasedDate": "2026-06-01T00:00:00.000Z",
            }
        ],
    }

    class _Resp:
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return sample

    monkeypatch.setattr(sr.requests, "get", lambda *a, **k: _Resp())

    jobs = sr.fetch_jobs(Employer(name="Bosch", ats="smartrecruiters", token="BoschGroup", country="de"))
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "Senior AI Engineer"
    assert j["source_board"] == "smartrecruiters"
    assert "smartrecruiters.com/BoschGroup/744000133914723" in j["url"]
    assert "Munich" in j["location"]


def test_unique_constraint_dedup():
    """The (company, title, url_hash) unique constraint blocks exact duplicates."""
    from sqlalchemy.exc import IntegrityError

    from honestapply.db.models import Job, url_hash
    from honestapply.db.session import session_scope

    url = "https://boards.greenhouse.io/acme/jobs/7"
    h = url_hash(url)
    with session_scope() as s:
        s.add(Job(company="Acme", title="AI Engineer", url=url, url_hash=h))

    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(Job(company="Acme", title="AI Engineer", url=url, url_hash=h))


def test_german_tech_hubs_are_kept():
    """ACCEPTANCE: boards render locations like "Freiburg, BW, DE" with no country word.

    These were dropped at discovery, silently discarding every posting from AI
    employers based outside the top-10 cities.
    """
    from honestapply.stages.discover import location_matches

    for loc in ("Freiburg, BW, DE", "Freiburg (Germany)", "Heidelberg", "Karlsruhe",
                "Aachen", "Erlangen", "Darmstadt", "Dresden"):
        assert location_matches(loc, []), f"{loc!r} should be kept"


def test_non_eu_locations_still_dropped():
    """The widened keep-list must not start admitting US/UK roles."""
    from honestapply.stages.discover import location_matches

    for loc in ("San Francisco, CA", "New York, NY", "London, UK"):
        assert not location_matches(loc, []), f"{loc!r} should be dropped"


def test_direct_apply_url_is_preferred_over_board_listing():
    """ACCEPTANCE: store the employer's real apply link, not the Indeed listing.

    jobspy returns both `job_url` (board listing) and `job_url_direct` (the
    employer's ATS posting). Preferring `job_url` meant Indeed/LinkedIn hosts
    were stored and then dropped by prefilter as dead hosts, discarding jobs
    whose direct link was perfectly appliable.
    """
    from honestapply.stages.discover import _best_url

    assert _best_url({
        "job_url": "https://de.indeed.com/viewjob?jk=1",
        "job_url_direct": "https://jobs.ashbyhq.com/acme/abc",
    }) == "https://jobs.ashbyhq.com/acme/abc"

    assert _best_url({
        "job_url": "https://www.linkedin.com/jobs/view/9",
        "job_url_direct": "https://boards.greenhouse.io/acme/jobs/1",
    }) == "https://boards.greenhouse.io/acme/jobs/1"


def test_falls_back_to_listing_when_no_usable_direct_url():
    """NaN/None/dead direct links must not blank out the URL."""
    from honestapply.stages.discover import _best_url

    listing = "https://de.indeed.com/viewjob?jk=2"
    assert _best_url({"job_url": listing, "job_url_direct": float("nan")}) == listing
    assert _best_url({"job_url": listing, "job_url_direct": None}) == listing
    # A direct link that is itself a dead host is no better than the listing.
    assert _best_url({"job_url": listing, "job_url_direct": "https://de.indeed.com/x"}) == listing
