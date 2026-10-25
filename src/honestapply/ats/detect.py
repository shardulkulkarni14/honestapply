"""ATS detection from a job URL (host-based)."""

from __future__ import annotations

from urllib.parse import urlparse

GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
WORKDAY = "workday"
SMARTRECRUITERS = "smartrecruiters"
LINKEDIN = "linkedin"
INSTAHYRE = "instahyre"
# Feed-based boards common with German employers (public JSON/XML, no auth).
PERSONIO = "personio"
RECRUITEE = "recruitee"
WORKABLE = "workable"
TEAMTAILOR = "teamtailor"
JOIN = "join"
# The Bundesagentur für Arbeit Jobbörse is a discovery source, not an apply
# channel: its listing pages hide the employer's contact/apply link behind an
# image CAPTCHA, so a job whose URL still points there has no automatable form.
ARBEITSAGENTUR = "arbeitsagentur"
# German/DACH portals that sit behind a mandatory account/login wall — no guest
# apply exists, so they can never be completed unattended.
UMANTIS = "umantis"
SUCCESSFACTORS = "successfactors"
GENERIC = "generic"

# ATS families that always require a human (portal account), regardless of
# market. Checked by the apply stage before a browser session is spent.
ACCOUNT_WALLED = frozenset({UMANTIS, SUCCESSFACTORS})


def detect_ats(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    full = (url or "").lower()

    if "greenhouse.io" in host or "boards.greenhouse.io" in full or "grnh.se" in host:
        return GREENHOUSE
    if "lever.co" in host:
        return LEVER
    if "ashbyhq.com" in host:
        return ASHBY
    if "myworkdayjobs.com" in host or "workday" in host:
        return WORKDAY
    if "smartrecruiters.com" in host:
        return SMARTRECRUITERS
    if "umantis.com" in host:
        return UMANTIS
    if "sapsf.eu" in host or "sapsf.com" in host or "successfactors" in host:
        return SUCCESSFACTORS
    if "linkedin.com" in host:
        return LINKEDIN
    if "instahyre.com" in host:
        return INSTAHYRE
    if "jobs.personio.de" in host or "jobs.personio.com" in host:
        return PERSONIO
    if host.endswith(".recruitee.com"):
        return RECRUITEE
    if "workable.com" in host:
        return WORKABLE
    if host.endswith(".teamtailor.com"):
        return TEAMTAILOR
    if host == "join.com" or host.endswith(".join.com"):
        return JOIN
    if host.endswith("arbeitsagentur.de"):
        return ARBEITSAGENTUR
    return GENERIC


def is_source_only(ats_type: str | None) -> bool:
    """True when the URL is a job-board listing with no automatable apply form
    (the Arbeitsagentur detail page). Such jobs are sourced, never applied to."""
    return (ats_type or "") == ARBEITSAGENTUR


def is_account_walled(ats_type: str | None) -> bool:
    """True if this ATS always needs a human (a portal account) to apply."""
    return (ats_type or "") in ACCOUNT_WALLED
