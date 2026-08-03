"""Structured resume schema (the source-of-truth YAML format).

Tailoring may reorder/rephrase bullets and pick a summary variant, but every
string inside `resume_facts` is IMMUTABLE. `iter_immutable_strings()` extracts
the exact substrings the tailor validator must find verbatim in any output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Contact(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""
    title: str = ""
    location: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""


class EducationItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    degree: str = ""
    institution: str = ""
    location: str = ""
    dates: str = ""
    details: str = ""


class ExperienceItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    company: str = ""
    title: str = ""
    dates: str = ""
    location: str = ""
    bullets: list[str] = Field(default_factory=list)


class ProjectItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""
    description: str = ""
    bullets: list[str] = Field(default_factory=list)


class ResumeFacts(BaseModel):
    """The immutable core. Tailoring must preserve every fact here verbatim."""

    model_config = ConfigDict(extra="allow")
    contact: Contact = Field(default_factory=Contact)
    education: list[EducationItem] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    skills: dict[str, Any] = Field(default_factory=dict)
    projects: list[ProjectItem] = Field(default_factory=list)
    awards: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)


class Resume(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    target_keywords: list[str] = Field(default_factory=list)
    resume_facts: ResumeFacts = Field(default_factory=ResumeFacts)
    summary_variants: list[str] = Field(default_factory=list)


def load_resume(path: str | Path) -> Resume:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    resume = Resume(**data)
    resume.__dict__["_source_path"] = str(path)
    return resume


def list_resumes(directory: str | Path) -> list[Resume]:
    directory = Path(directory)
    out: list[Resume] = []
    if not directory.exists():
        return out
    for p in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
        try:
            out.append(load_resume(p))
        except Exception:  # pragma: no cover - skip malformed files
            continue
    return out


# ---------------------------------------------------------------------------
# Immutable-fact extraction (used by the tailor validator)
# ---------------------------------------------------------------------------
def _walk_strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        s = value.strip()
        if s:
            found.append(s)
    elif isinstance(value, dict):
        for v in value.values():
            found.extend(_walk_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            found.extend(_walk_strings(v))
    elif isinstance(value, BaseModel):
        found.extend(_walk_strings(value.model_dump()))
    return found


def iter_immutable_strings(facts: ResumeFacts | dict[str, Any]) -> list[str]:
    """Every non-empty string inside resume_facts. The tailor output must
    contain each of these as an exact substring (after light normalization)."""
    data = facts.model_dump() if isinstance(facts, ResumeFacts) else facts
    # Dedup while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for s in _walk_strings(data):
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


def keyword_match_score(resume: Resume, jd_text: str) -> tuple[int, int]:
    """(num_matches, longest_match_len) for ranking base resumes against a JD.
    Ties in the original spec break by longest matched keyword."""
    jd = (jd_text or "").lower()
    matches = [kw for kw in resume.target_keywords if kw.lower() in jd]
    longest = max((len(kw) for kw in matches), default=0)
    return len(matches), longest
