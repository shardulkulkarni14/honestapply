"""Tailoring stage: resume selection, the immutable-facts validator
(the deliberate-fabrication acceptance test), and end-to-end tailoring."""

from __future__ import annotations

from pathlib import Path

import pytest

RESUME_DIR = Path("data/resumes")
DEFAULT_RESUME = RESUME_DIR / "default.yaml"


def _have_resumes() -> bool:
    return DEFAULT_RESUME.exists()


pytestmark = pytest.mark.skipif(
    not _have_resumes(), reason="resume YAMLs not present in data/resumes/"
)


def test_keyword_match_prefers_relevant_resume():
    from honestapply.resume.schema import keyword_match_score, list_resumes

    resumes = list_resumes(RESUME_DIR)
    assert resumes, "expected at least one base resume"
    jd = "We need an AI engineer with python, llm, rag, langchain, langgraph, fastapi."
    best = max(resumes, key=lambda r: keyword_match_score(r, jd))
    n, _ = keyword_match_score(best, jd)
    assert n > 0


def test_immutable_facts_validator_passes_on_faithful_copy():
    from honestapply.resume.schema import load_resume
    from honestapply.stages.tailor import validate_immutable_facts

    resume = load_resume(DEFAULT_RESUME)
    faithful = DEFAULT_RESUME.read_text(encoding="utf-8")  # all facts present verbatim
    missing = validate_immutable_facts(resume, faithful)
    assert missing == [], f"unexpected missing facts: {missing}"


def test_immutable_facts_validator_catches_fabrication():
    """ACCEPTANCE: changing a real metric must be detected as a missing fact."""
    from honestapply.resume.schema import iter_immutable_strings, load_resume
    from honestapply.stages.tailor import validate_immutable_facts

    resume = load_resume(DEFAULT_RESUME)
    original = DEFAULT_RESUME.read_text(encoding="utf-8")

    # Pick any immutable fact from the loaded resume rather than hardcoding one:
    # the resume YAML is user-supplied and gitignored, so a fixed metric would
    # only pass on one person's file.
    fact = next(
        (f for f in iter_immutable_strings(resume.resume_facts) if f and f in original),
        None,
    )
    assert fact, "resume declares no immutable fact present verbatim in the file"

    fabricated = original.replace(fact, "something the resume never claimed")
    missing = validate_immutable_facts(resume, fabricated)
    assert missing, "validator FAILED to catch a fabricated fact"
    assert any(fact in m for m in missing)


def test_run_tailor_produces_pdf(add_job):
    import os

    from honestapply.db.models import Job, Status
    from honestapply.db.session import session_scope
    from honestapply.stages.tailor import run_tailor

    jid = add_job(status=Status.SCORED)
    n = run_tailor()
    assert n == 1
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == Status.TAILORED
        assert j.tailored_resume_path and os.path.exists(j.tailored_resume_path)
