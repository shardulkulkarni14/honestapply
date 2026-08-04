"""Deterministic classification of a posting into grouping dimensions.

These feed analytics ("response rate by role_family") and prefilter. They are
computed with keyword rules rather than an LLM on purpose: it is free, runs at
discovery scale, is reproducible, and — unlike a model — cannot invent a category
that isn't supported by the text. A miss just leaves the field ``None``, which
analytics buckets as "unclassified"; nothing depends on perfect coverage.
"""

from __future__ import annotations

import re

# Role families, checked in order — the first match wins, so put the most
# specific first. "Applied AI / GenAI" outranks generic "software" because a
# GenAI engineer role should not be filed under SWE just for containing
# "engineer".
_ROLE_FAMILY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("genai", re.compile(r"\b(genai|gen ai|generative ai|llm|rag|agent(ic)?|prompt)\b", re.I)),
    ("ml", re.compile(r"\b(machine learning|ml|mlops|deep learning|computer vision|nlp|"
                      r"model training|pre-?training|research (engineer|scientist))\b", re.I)),
    ("data", re.compile(r"\b(data scien(ce|tist)|data engineer|analytics engineer|"
                        r"applied scien(ce|tist))\b", re.I)),
    ("platform", re.compile(r"\b(platform engineer|infrastructure|devops|\bsre\b|"
                            r"site reliability|cloud engineer)\b", re.I)),
    ("frontend", re.compile(r"\b(frontend|front-end|react|ui engineer)\b", re.I)),
    ("backend", re.compile(r"\b(backend|back-end|api engineer)\b", re.I)),
    ("fullstack", re.compile(r"\b(full[- ]?stack)\b", re.I)),
    ("swe", re.compile(r"\b(software engineer|software developer|programmer)\b", re.I)),
]

_SENIORITY_RULES: list[tuple[str, re.Pattern[str]]] = [
    # Order matters: "senior staff" should read as staff, "lead" before "senior".
    ("intern", re.compile(r"\b(intern|internship|working student|werkstudent)\b", re.I)),
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    ("staff", re.compile(r"\b(staff)\b", re.I)),
    ("lead", re.compile(r"\b(lead|head of|manager|director|vp|chief)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|entry[- ]level|graduate|associate)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|principal)\b", re.I)),
]

_REMOTE = re.compile(r"\b(remote|work from home|wfh|home office|fully distributed)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(r"\b(on-?site|in-?office|in person|vor ort)\b", re.I)


def classify_role_family(title: str) -> str | None:
    for name, pattern in _ROLE_FAMILY_RULES:
        if pattern.search(title or ""):
            return name
    return None


def classify_seniority(title: str) -> str | None:
    for name, pattern in _SENIORITY_RULES:
        if pattern.search(title or ""):
            return name
    return None  # unmarked titles are usually "mid" but we don't guess


def classify_work_model(location: str, description: str | None = "") -> str | None:
    """Remote/hybrid/onsite from the location field first, then the JD body.

    Hybrid is checked before remote because "hybrid remote" postings are hybrid;
    an explicit onsite marker only wins if neither of the others appears.
    """
    haystack = f"{location or ''}\n{description or ''}"
    if _HYBRID.search(haystack):
        return "hybrid"
    if _REMOTE.search(haystack):
        return "remote"
    if _ONSITE.search(haystack):
        return "onsite"
    return None


def classify(title: str, location: str = "", description: str | None = "") -> dict[str, str | None]:
    """All three dimensions at once, for populating a Job at score time."""
    return {
        "role_family": classify_role_family(title),
        "seniority": classify_seniority(title),
        "work_model": classify_work_model(location, description),
    }
