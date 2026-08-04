"""Outcome analytics and the deterministic taxonomy classifier."""

from __future__ import annotations

from datetime import timedelta

import pytest

from honestapply.analytics import compute
from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.taxonomy import classify_role_family, classify_seniority, classify_work_model


# --- taxonomy classifier ----------------------------------------------------
@pytest.mark.parametrize(
    "title,expected",
    [
        ("Senior GenAI Engineer (RAG/LLM)", "genai"),
        ("Machine Learning Engineer, Computer Vision", "ml"),
        ("Data Scientist", "data"),
        ("Platform Engineer (Kubernetes)", "platform"),
        ("Senior Software Engineer", "swe"),
        ("Head of Marketing", None),
    ],
)
def test_role_family_classification(title, expected):
    assert classify_role_family(title) == expected


def test_genai_outranks_swe():
    # A GenAI role that also says "engineer" must not be filed as generic SWE.
    assert classify_role_family("LLM Software Engineer") == "genai"


@pytest.mark.parametrize(
    "title,expected",
    [("Senior AI Engineer", "senior"), ("Lead Data Scientist", "lead"),
     ("Working Student ML", "intern"), ("Staff Engineer", "staff")],
)
def test_seniority_classification(title, expected):
    assert classify_seniority(title) == expected


def test_work_model_prefers_hybrid_over_remote():
    assert classify_work_model("Berlin (hybrid remote)") == "hybrid"
    assert classify_work_model("Remote - EU") == "remote"
    assert classify_work_model("Munich, on-site") == "onsite"
    assert classify_work_model("Munich") is None


# --- analytics --------------------------------------------------------------
def _apply(jid: int, when):
    """Drive a job to APPLIED at time `when` by writing events directly."""
    from honestapply.db.models import JobEvent

    with session_scope() as s:
        s.add(JobEvent(job_id=jid, from_status=Status.COVERED, to_status=Status.APPLIED, at=when))


def _advance(jid: int, to_status, when):
    from honestapply.db.models import JobEvent

    with session_scope() as s:
        s.add(JobEvent(job_id=jid, to_status=to_status, at=when))
        s.get(Job, jid).status = to_status


def test_empty_history_gives_empty_but_valid_analytics():
    with session_scope() as s:
        data = compute(s)
    assert data["applied_total"] == 0
    assert data["overall"]["interview_rate"] is None
    assert data["recommended_min_score"] is None


def test_rates_and_time_to_response(add_job):
    from datetime import datetime

    base = datetime(2026, 1, 1)
    # Two Greenhouse jobs: one interviews (7 days later), one is rejected.
    a = add_job(company="A", ats_type="greenhouse", score=9,
                url="https://boards.greenhouse.io/x/jobs/1")
    b = add_job(company="B", ats_type="greenhouse", score=6,
                url="https://boards.greenhouse.io/y/jobs/2")
    # One Lever job that ghosts (applied, no response).
    c = add_job(company="C", ats_type="lever", score=8,
                url="https://jobs.lever.co/z/3")

    for jid in (a, b, c):
        _apply(jid, base)
    _advance(a, Status.INTERVIEWING, base + timedelta(days=7))
    _advance(b, Status.REJECTED, base + timedelta(days=3))
    # c stays applied → no response

    with session_scope() as s:
        data = compute(s)

    assert data["applied_total"] == 3
    assert data["no_response"] == 1  # the ghosted Lever job
    assert data["overall"]["positive"] == 1  # A interviewed
    assert data["overall"]["responded"] == 2  # A + B (rejection is a response)
    assert data["overall"]["interview_rate"] == round(1 / 3, 3)
    assert data["overall"]["median_days_to_response"] == 5.0  # median(7, 3)

    # Greenhouse got both responses; Lever got none — the sourcing signal.
    assert data["by_ats"]["greenhouse"]["responded"] == 2
    assert data["by_ats"]["lever"]["responded"] == 0
