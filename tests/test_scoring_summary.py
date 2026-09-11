"""The candidate block of the score prompt must carry skills and experience.

Regression: `Profile.summary_for_scoring()` used to return only name, locations,
work authorization and salary. The score prompt asks the model to match the
candidate's *skills and experience* against the posting — which it was never
given, so every fit score was a guess. These tests pin that the résumé's
background is present, and that the score stage actually sends it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from honestapply.config import PATHS, Profile

EXAMPLE = Path(PATHS.root) / "config" / "resume.example.yaml"

# Strings that exist verbatim in the tracked example résumé.
EXPECTED_FROM_RESUME = [
    "Acme Logistics GmbH",       # experience: company
    "Senior Backend Engineer",   # experience: title
    "Startup Labs UG",           # second role
    "FastAPI",                   # skills group content
    "Cloud / DevOps",            # skills group name
    "M.Sc. Computer Science",    # education
    "microservices",             # target keyword
    "Backend engineer with 4+ years",  # summary variant
]


def _resume():
    from honestapply.resume.schema import load_resume

    return load_resume(EXAMPLE)


def test_summary_contains_experience_and_skills():
    profile = Profile(legal_name={"first": "Alex", "last": "Example"},
                      preferred_locations=["Berlin"])
    text = profile.summary_for_scoring(_resume())

    # The original fields are still there...
    assert "Name: Alex Example" in text
    assert "Preferred locations: Berlin" in text
    assert "Work authorization:" in text and "Salary expectation:" in text
    # ...and now the background the prompt was always asking about.
    for needle in EXPECTED_FROM_RESUME:
        assert needle in text, f"scoring summary lacks {needle!r}"
    for section in ("Headline:", "Summary:", "Skills:", "Experience", "Education:",
                    "Languages:", "Target keywords:"):
        assert section in text
    # Compact: the posting is the long part of the prompt.
    assert len(text) < 4000


def test_summary_only_surfaces_what_the_resume_says():
    """No synthesis: every experience/skills line is lifted from the YAML."""
    resume = _resume()
    text = Profile().summary_for_scoring(resume)
    for exp in resume.resume_facts.experience:
        assert exp.company in text and exp.title in text
        assert exp.bullets[0][:60] in text  # first bullet, verbatim prefix
    for group, items in resume.resume_facts.skills.items():
        assert group in text and str(items)[:40] in text


def test_summary_resolves_default_resume_from_paths(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "data" / "resumes").mkdir(parents=True)
    shutil.copy(EXAMPLE, root / "data" / "resumes" / "default.yaml")
    monkeypatch.setattr(PATHS, "root", root)

    text = Profile().summary_for_scoring()  # no résumé passed: found via PATHS
    assert "Acme Logistics GmbH" in text and "Skills:" in text


def test_summary_falls_back_to_profile_skills_without_a_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(PATHS, "root", tmp_path / "empty")  # no data/resumes at all
    profile = Profile(
        professional_summary="Backend engineer, 5 years of Python.",
        skills={"_note": "ignored", "languages": ["Python", "SQL"], "cloud": "AWS, Docker"},
    )
    text = profile.summary_for_scoring()
    assert "Backend engineer, 5 years of Python." in text
    assert "languages: Python, SQL" in text and "cloud: AWS, Docker" in text
    assert "_note" not in text


def test_placeholder_profile_values_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(PATHS, "root", tmp_path / "empty")
    profile = Profile(professional_summary="TODO_FILL_IN — 2-3 sentences", skills={"x": "TODO"})
    text = profile.summary_for_scoring()
    assert "TODO" not in text


@pytest.mark.usefixtures("fresh_db")
def test_score_stage_sends_the_background_to_the_llm(add_job, tmp_path, monkeypatch):
    """End to end: the prompt the score stage builds carries the résumé content."""
    from honestapply.llm.base import StubProvider
    from honestapply.stages import score as score_stage

    root = tmp_path / "root"
    (root / "data" / "resumes").mkdir(parents=True)
    shutil.copy(EXAMPLE, root / "data" / "resumes" / "default.yaml")
    monkeypatch.setattr(PATHS, "root", root)

    seen: list[str] = []

    class Recording(StubProvider):
        def complete(self, prompt, **kw):
            seen.append(prompt)
            return super().complete(prompt, **kw)

    monkeypatch.setattr(score_stage, "get_score_provider", lambda settings=None: Recording())
    add_job()
    assert score_stage.run_score() == 1
    assert seen, "score stage made no LLM call"
    prompt = seen[0]
    assert "## Candidate Profile" in prompt
    for needle in ("Acme Logistics GmbH", "Senior Backend Engineer", "FastAPI", "Skills:"):
        assert needle in prompt
    assert prompt.index("Acme Logistics GmbH") < prompt.index("## Job Details")
