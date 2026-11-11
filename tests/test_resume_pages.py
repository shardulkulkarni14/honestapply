"""Résumé page budget: compact re-render, trim-to-fit, and the hard stop."""

from __future__ import annotations

import pytest

from honestapply.resume.renderer import ResumeTooLongError, count_pdf_pages, render_resume_pdf
from honestapply.resume.schema import Resume, ResumeFacts
from honestapply.stages.tailor import render_within_budget, shrink_steps

pytest.importorskip("weasyprint")


def _facts(n_primary: int, n_other: int = 3, n_projects: int = 4) -> dict:
    bullet = "Built and shipped a production service with tests, CI/CD and monitoring, used daily by customers."
    return {
        "contact": {"name": "Test Person", "email": "test@example.com", "phone": "+49 000", "location": "Munich"},
        "skills": {f"Group {i}": [f"skill-{i}-{j}" for j in range(7)] for i in range(6)},
        "experience": [
            {"company": "Primary Co", "title": "Engineer", "location": "Munich",
             "start_date": "2024-01", "end_date": "Present", "bullets": [f"{bullet} ({i})" for i in range(n_primary)]},
            {"company": "Other Co", "title": "Engineer", "location": "Berlin",
             "start_date": "2022-01", "end_date": "2023-12", "bullets": [f"{bullet} ({i})" for i in range(n_other)]},
        ],
        "education": [{"degree": "M.Sc.", "institution": "University", "start_date": "2020", "end_date": "2022"}],
        "projects": [
            {"name": f"Project {i}", "description": "demo", "optional": i > 0,
             "bullets": [f"{bullet} p{i}-{j}" for j in range(2)]}
            for i in range(n_projects)
        ],
        "awards": ["Award one", "Award two", "Award three", "Award four"],
        "languages": ["English (C1)"],
        "certifications": [],
    }


def _resume(**kw) -> Resume:
    return Resume(name="t", target_keywords=["python"], resume_facts=ResumeFacts(**_facts(**kw)),
                  summary_variants=["Summary text."])


def test_short_resume_fits_without_changes(tmp_path):
    r = _resume(n_primary=4, n_other=2, n_projects=2)
    out = render_resume_pdf(r, tmp_path / "r.pdf", summary="S", max_pages=2)
    assert count_pdf_pages(out) <= 2


def test_overlong_resume_raises_when_compact_is_not_enough(tmp_path):
    r = _resume(n_primary=60, n_other=20, n_projects=6)
    with pytest.raises(ResumeTooLongError) as exc:
        render_resume_pdf(r, tmp_path / "r.pdf", summary="S", max_pages=2)
    assert exc.value.pages > 2 and exc.value.max_pages == 2


def test_shrink_steps_only_drop_whole_items():
    facts = _facts(n_primary=12, n_other=5, n_projects=4)
    versions = list(shrink_steps(facts))
    assert versions, "expected at least one trim step"
    original = {b for e in facts["experience"] for b in e["bullets"]}
    for v in versions:
        kept = {b for e in v["experience"] for b in e["bullets"]}
        assert kept <= original  # never a new or edited string
        assert all(e["bullets"] for e in v["experience"])  # never an empty role
        assert v["projects"][0]["name"] == "Project 0"  # non-optional project stays
    last = versions[-1]
    assert len(last["experience"][0]["bullets"]) <= 5
    assert len(last["awards"]) <= 2


def test_render_within_budget_trims_until_it_fits(tmp_path):
    r = _resume(n_primary=60, n_other=20, n_projects=6)
    fitted, out, steps = render_within_budget(r, tmp_path / "r.pdf", summary="S", max_pages=2)
    assert steps > 0
    assert count_pdf_pages(out) <= 2
    assert len(fitted.resume_facts.experience[0].bullets) < 60
    assert all(e.bullets for e in fitted.resume_facts.experience)


def test_budget_zero_disables_check(tmp_path):
    r = _resume(n_primary=60, n_other=20, n_projects=6)
    fitted, out, steps = render_within_budget(r, tmp_path / "r.pdf", summary="S", max_pages=0)
    assert steps == 0 and count_pdf_pages(out) > 2
