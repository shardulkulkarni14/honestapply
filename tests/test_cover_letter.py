"""Cover-letter stage: the language-fabrication guard + end-to-end stub run."""

from __future__ import annotations

from pathlib import Path

import pytest

RESUME = Path("data/resumes/default.yaml")
pytestmark = pytest.mark.skipif(not RESUME.exists(), reason="resume YAML not present")


def _resume():
    from honestapply.resume.schema import load_resume

    return load_resume(RESUME)


def test_guard_catches_native_german_fabrication():
    """ACCEPTANCE: claiming 'native German' when the candidate is A2 must be flagged."""
    from honestapply.stages.cover_letter import validate_cover_letter

    body = "I am a native German speaker with fluent English, ready to help your team."
    issues = validate_cover_letter(body, _resume())
    assert issues, "guard failed to catch 'native German' over-claim"
    assert any("german" in i.lower() for i in issues)


def test_guard_allows_truthful_language_claims():
    """No false positive: 'fluent English' is fine for a C1 English speaker."""
    from honestapply.stages.cover_letter import validate_cover_letter

    body = "I bring fluent English and working German (A2, improving) to the role."
    assert validate_cover_letter(body, _resume()) == []


def test_run_cover_letters_stub(add_job):
    from honestapply.db.models import Job, Status
    from honestapply.db.session import session_scope
    from honestapply.stages.cover_letter import run_cover_letters

    # needs a TAILORED job with a matched resume to work from
    jid = add_job(status=Status.TAILORED, matched_resume_path=str(RESUME))
    n = run_cover_letters()
    assert n == 1
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == Status.COVERED
        assert j.cover_letter_path and Path(j.cover_letter_path).exists()


# --- model-preamble guard --------------------------------------------------
# Regression: a real run rendered the model's own commentary about its honesty
# constraints into the PDF that would have been sent to the employer.

def test_preamble_guard_strips_model_commentary():
    """ACCEPTANCE: model commentary before a `---` rule must not reach the PDF."""
    from honestapply.stages.cover_letter import strip_model_preamble

    body = (
        "Every string in `resume_facts` is immutable and the tailor validator\n"
        "substring-checks it. Here's the draft:\n\n"
        "---\n\n"
        "I'm writing to apply for the AI Engineer role at Acme.\n\n"
        "Alex Rivera"
    )
    out = strip_model_preamble(body)
    assert "resume_facts" not in out
    assert "Here's the draft" not in out
    assert out.startswith("I'm writing to apply")
    assert out.rstrip().endswith("Alex Rivera")


def test_preamble_guard_leaves_a_clean_letter_untouched():
    """A letter with no commentary must survive byte-for-byte."""
    from honestapply.stages.cover_letter import strip_model_preamble

    body = (
        "Dear Hiring Team,\n\n"
        "I'm writing to apply for the AI Engineer role.\n\n"
        "Sincerely,\nAlex Rivera"
    )
    assert strip_model_preamble(body) == body


def test_preamble_guard_strips_trailing_offer_to_revise():
    """Commentary after the sign-off must go too."""
    from honestapply.stages.cover_letter import strip_model_preamble

    body = (
        "I'm writing to apply for the role at Acme.\n\n"
        "Alex Rivera\n\n"
        "Let me know if you'd like me to adjust the tone."
    )
    out = strip_model_preamble(body)
    assert "Let me know" not in out
    assert out.rstrip().endswith("Alex Rivera")


# --- task-narration leaks ----------------------------------------------------
# The fixed phrase list missed real leaks observed in production: one draft opened
# "I'll draft I need to reason: candidate summary + JD + honesty constraints, then
# produce only the letter body.", and another opened "Cover letter body:" and was
# submitted that way. Detect the model narrating the task, not known phrases.

def test_strips_task_narration_before_salutation():
    from honestapply.stages.cover_letter import strip_model_preamble

    body = (
        "I'll draft I need to reason: candidate summary + JD + honesty "
        "constraints, then produce only the letter body.\n"
        "Dear Northwind Team,\n\nNorthwind's premise matters to me.\n\nAlex Rivera"
    )
    out = strip_model_preamble(body)
    assert out.startswith("Dear Northwind Team")
    assert "I need to reason" not in out
    assert "honesty constraints" not in out


def test_hook_opening_is_never_truncated():
    """A letter may open with a hook and say 'I'm writing' later — keep it whole.

    An early fix used a length heuristic here and would have deleted the
    strongest paragraph of letters opening this way.
    """
    from honestapply.stages.cover_letter import strip_model_preamble

    body = (
        "Forward Deployed Engineering at Northwind is a natural fit for how I already "
        "work: sitting beside customers and shipping systems that survive production.\n\n"
        "I'm writing to apply because that is the mandate I look for.\n\nAlex Rivera"
    )
    assert strip_model_preamble(body) == body.strip()
