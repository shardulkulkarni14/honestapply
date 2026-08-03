"""Tests for the cheap no-LLM relevance prefilter."""

from __future__ import annotations

from honestapply.stages.prefilter import is_relevant


def test_keeps_relevant_engineering_roles():
    keep, _ = is_relevant("Senior AI Engineer", "https://boards.greenhouse.io/acme/jobs/1", "Berlin")
    assert keep is True
    keep, _ = is_relevant("Machine Learning Engineer", "https://jobs.smartrecruiters.com/Acme/2", "Munich")
    assert keep is True
    # plain/ambiguous titles are kept — the LLM gate decides
    keep, _ = is_relevant("Data Scientist", "https://jobs.ashbyhq.com/acme/3", "Remote")
    assert keep is True


def test_drops_dead_hosts():
    keep, reason = is_relevant("AI Engineer", "https://de.indeed.com/viewjob?jk=123", "Berlin")
    assert keep is False and "indeed" in reason.lower()
    keep, reason = is_relevant("AI Engineer", "https://www.linkedin.com/jobs/view/123", "Berlin")
    assert keep is False and "linkedin" in reason.lower()


def test_drops_clearly_irrelevant_titles():
    for title in [
        "Werkstudent Marketing (m/w/d)",
        "Praktikum im Produktmanagement",
        "Station Agent - Acme Transit",
        "Customer Service Representative",
        "Masterarbeit Simulative Fahrwerksanalyse",
    ]:
        keep, reason = is_relevant(title, "https://boards.greenhouse.io/acme/jobs/9", "Berlin")
        assert keep is False, f"should drop: {title}"
        assert reason


# --- pipeline-level regressions (2026-07-31) --------------------------------

def test_prefilter_scans_beyond_discovered():
    """ACCEPTANCE: a dead-host job already ENRICHED must still be reachable.

    Prefilter originally filtered on status == DISCOVERED, so jobs enriched by
    an earlier run kept their Indeed/LinkedIn URL and burned four LLM calls
    before the apply stage rejected them.
    """
    from honestapply.stages.prefilter import PRE_APPLY_STATUSES
    from honestapply.db.models import Status

    for st in (Status.DISCOVERED, Status.ENRICHED, Status.SCORED, Status.TAILORED):
        assert st in PRE_APPLY_STATUSES


def test_dead_hosts_still_dropped():
    """The dead-host rule must survive the 2026-07-31 prefilter relaxation."""
    from honestapply.stages.prefilter import is_relevant

    keep, reason = is_relevant("AI Engineer", "https://de.indeed.com/viewjob?jk=1")
    assert not keep and "dead host" in reason
    keep, _ = is_relevant("AI Engineer", "https://job-boards.greenhouse.io/acme/jobs/1")
    assert keep


def test_ai_lab_titles_are_no_longer_prefiltered():
    """Relaxation: AI-lab role types reach the LLM score gate instead of being dropped.

    AI labs post titles like "Member of Technical Staff - Pretraining"; the old
    rejection-correlated rule dropped every one before scoring.
    """
    from honestapply.stages.prefilter import is_relevant

    for title in (
        "Member of Technical Staff - Pretraining",
        "Member of Technical Staff - Post Training",
        "Forward Deployed Robotics Engineer",
        "Platform Engineer",
    ):
        keep, reason = is_relevant(title, "https://job-boards.greenhouse.io/acme/jobs/1")
        assert keep, f"{title!r} should reach the score gate, dropped as: {reason}"


def test_obvious_junk_still_dropped():
    """Relaxing must not let plainly non-engineering titles through."""
    from honestapply.stages.prefilter import is_relevant

    for title in ("Werkstudent Marketing", "Warehouse Cleaner", "Data Science Intern"):
        keep, _ = is_relevant(title, "https://job-boards.greenhouse.io/acme/jobs/1")
        assert not keep, f"{title!r} should still be dropped"
