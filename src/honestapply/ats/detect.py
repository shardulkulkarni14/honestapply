"""ATS detection from a job URL (host-based)."""

from __future__ import annotations

from urllib.parse import urlparse

GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
WORKDAY = "workday"
SMARTRECRUITERS = "smartrecruiters"
LINKEDIN = "linkedin"
GENERIC = "generic"


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
    if "linkedin.com" in host:
        return LINKEDIN
    return GENERIC
