"""Location-specific base résumés (relocation headers) must only be picked for
jobs in that location. Regression: a Munich posting was tailored from an
India-relocation variant, so the PDF header read "Relocating to Hyderabad,
India" with the +91 number."""

from __future__ import annotations

from honestapply.resume.schema import Contact, Resume, ResumeFacts
from honestapply.stages.tailor import relocation_target, resume_fits_job_location


def _resume(name: str, location: str, keywords: list[str] | None = None) -> Resume:
    return Resume(
        name=name,
        target_keywords=keywords or ["python"],
        resume_facts=ResumeFacts(contact=Contact(name="Test", location=location)),
    )


def test_relocation_target_parsing():
    assert relocation_target("Relocating to Hyderabad, India (open to on-site)") == "Hyderabad, India"
    assert relocation_target("Ingolstadt, Germany · Open to relocating to Abu Dhabi, UAE") == "Abu Dhabi, UAE"
    assert relocation_target("Ingolstadt / Munich, Germany (open to relocating to Berlin)") == "Berlin"
    assert relocation_target("Ingolstadt / Munich, Germany") is None
    assert relocation_target("") is None


def test_neutral_resume_fits_everywhere():
    neutral = _resume("default", "Ingolstadt / Munich, Germany")
    assert resume_fits_job_location(neutral, "München, BY, DE")
    assert resume_fits_job_location(neutral, "Pune, India")
    assert resume_fits_job_location(neutral, "")


def test_india_variant_excluded_for_munich_job():
    india = _resume("india_ai", "Relocating to Hyderabad, India (open to on-site)")
    assert not resume_fits_job_location(india, "München, BY, DE")
    assert not resume_fits_job_location(india, "Munich, Germany (hybrid)")
    assert not resume_fits_job_location(india, "")


def test_india_variant_allowed_for_india_job():
    india = _resume("india_ai", "Relocating to Hyderabad, India (open to on-site)")
    assert resume_fits_job_location(india, "Hyderabad, Telangana")
    assert resume_fits_job_location(india, "Bengaluru, India")
    assert resume_fits_job_location(india, "Pune, MH, IN")  # trailing ISO code


def test_uae_variant_excluded_for_german_job_but_allowed_in_uae():
    uae = _resume("aiq", "Ingolstadt, Germany · Open to relocating to Abu Dhabi, UAE")
    assert not resume_fits_job_location(uae, "Gilching, BY, DE")
    assert resume_fits_job_location(uae, "Abu Dhabi, United Arab Emirates")
    assert resume_fits_job_location(uae, "Dubai, AE")


def test_tailor_one_never_picks_relocation_variant_for_munich(add_job, monkeypatch):
    """End-to-end on the selection step: even when the India variant wins on
    keywords, the Munich job must be tailored from a location-neutral base."""
    from honestapply.db.models import Job, Status
    from honestapply.db.session import session_scope
    from honestapply.stages import tailor as tailor_mod

    neutral = _resume("default", "Ingolstadt / Munich, Germany", ["python"])
    india = _resume(
        "india_ai",
        "Relocating to Hyderabad, India (open to on-site)",
        ["python", "llm", "rag", "langchain"],
    )

    chosen: dict[str, str] = {}

    def fake_prompt(name, **kwargs):  # capture which base résumé reached the prompt
        chosen["yaml"] = kwargs.get("resume_yaml", "")
        raise RuntimeError("stop after selection")

    monkeypatch.setattr(tailor_mod, "_load_prompt", fake_prompt)

    jid = add_job(status=Status.SCORED, location="München, BY, DE",
                  description="python llm rag langchain")
    with session_scope() as s:
        job = s.get(Job, jid)
        try:
            tailor_mod._tailor_one(job, provider=None, resumes=[india, neutral])
        except RuntimeError:
            pass
    assert "name: default" in chosen["yaml"]
    assert "Hyderabad" not in chosen["yaml"]
