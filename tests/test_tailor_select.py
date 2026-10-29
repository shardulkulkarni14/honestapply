"""Select-mode tailoring (`tailoring: select`): the base résumé is a master
superset and the LLM may drop bullets, projects and skill items — but every
kept string must be a verbatim copy, in the same entry, and nothing
non-optional may disappear. Synthetic résumé: no user data needed."""

from __future__ import annotations

import copy

import pytest
import yaml

MASTER = {
    "name": "master",
    "tailoring": "select",
    "target_keywords": ["python", "llm", "engineer"],
    "summary_variants": ["Engineer who ships.", "Engineer who leads."],
    "resume_facts": {
        "contact": {"name": "A. Tester", "email": "a@example.com", "location": "Berlin, DE"},
        "education": [
            {"degree": "M.Sc. Something", "institution": "Some University", "dates": "2020 - 2022"}
        ],
        "experience": [
            {
                "company": "Acme",
                "title": "AI Engineer",
                "dates": "2023 - 2025",
                "location": "Berlin",
                "bullets": [
                    "Built a RAG system serving 1,000 users.",
                    "Cut inference latency by 37%.",
                    "Ran the Linux servers and the NAS.",
                ],
            },
            {
                "company": "Beta",
                "title": "Intern",
                "dates": "2019",
                "location": "Pune",
                "optional": True,
                "bullets": ["Wrote ETL jobs."],
            },
        ],
        "skills": {
            "Backend": ["Python", "FastAPI", "PostgreSQL"],
            "Ops": "Docker, CI/CD, Linux",
        },
        "projects": [
            {"name": "Core", "description": "Main thing", "bullets": ["Core bullet one.", "Core bullet two."]},
            {"name": "Side", "description": "Side thing", "optional": True, "bullets": ["Side bullet."]},
        ],
        "awards": ["Hackathon finalist 2020"],
        "languages": ["English (C1)", "German (A2)"],
        "certifications": ["Cert A", "Cert B"],
    },
}


def _resume():
    from honestapply.resume.schema import Resume

    return Resume(**copy.deepcopy(MASTER))


def _good_selection() -> dict:
    """A legitimate subset: 2 of 3 Acme bullets, optional entries dropped,
    skills trimmed, one cert dropped, summary rewritten, bold markers added."""
    out = copy.deepcopy(MASTER)
    f = out["resume_facts"]
    f["experience"] = [
        {
            "company": "Acme", "title": "AI Engineer", "dates": "2023 - 2025", "location": "Berlin",
            "bullets": ["Cut inference latency by **37%**.", "Built a RAG system serving 1,000 users."],
        }
    ]
    f["skills"] = {"Backend": ["Python", "FastAPI"], "Ops": "Docker, Linux"}
    f["projects"] = [{"name": "Core", "description": "Main thing", "bullets": ["Core bullet two."]}]
    f["certifications"] = ["Cert B"]
    out["summary_variants"] = ["Engineer who ships RAG systems."]
    return out


def test_faithful_subset_is_accepted():
    from honestapply.stages.tailor import validate_selection

    assert validate_selection(_resume(), _good_selection()) == []


def test_echo_of_master_is_accepted():
    from honestapply.stages.tailor import validate_selection

    assert validate_selection(_resume(), copy.deepcopy(MASTER)) == []


@pytest.mark.parametrize(
    "mutate, expect",
    [
        # reworded bullet (fabrication / alteration)
        (lambda f: f["experience"][0]["bullets"].__setitem__(0, "Cut inference latency by 50%."),
         "bullet not in master entry"),
        # brand-new bullet
        (lambda f: f["experience"][0]["bullets"].append("Led a team of 40."),
         "bullet not in master entry"),
        # bullet moved from the optional Beta entry into Acme
        (lambda f: f["experience"][0]["bullets"].append("Wrote ETL jobs."),
         "bullet not in master entry"),
        # required entry dropped
        (lambda f: f.__setitem__("experience", []), "required entry dropped"),
        # entry kept but stripped of bullets
        (lambda f: f["experience"][0].__setitem__("bullets", []), "no bullets kept"),
        # changed dates on a kept entry
        (lambda f: f["experience"][0].__setitem__("dates", "2022 - 2025"), "entry not in master"),
        # changed location on a kept entry
        (lambda f: f["experience"][0].__setitem__("location", "Munich"), "differs from master"),
        # new skill item
        (lambda f: f["skills"]["Backend"].append("Kubernetes"), "item not in master"),
        # skill item moved between groups
        (lambda f: f["skills"]["Backend"].append("Docker"), "item not in master"),
        # new skills group
        (lambda f: f["skills"].__setitem__("Leadership", ["Hiring"]), "group not in master"),
        # required project dropped
        (lambda f: f.__setitem__("projects", []), "required entry dropped"),
        # new award
        (lambda f: f["awards"].append("Nobel Prize"), "awards: not in master"),
        # language level changed
        (lambda f: f.__setitem__("languages", ["English (C1)", "German (C1)"]), "languages"),
        # contact altered
        (lambda f: f["contact"].__setitem__("location", "Munich, DE"), "contact"),
        # education dropped
        (lambda f: f.__setitem__("education", []), "education: missing"),
    ],
)
def test_violations_are_rejected(mutate, expect):
    from honestapply.stages.tailor import validate_selection

    out = _good_selection()
    mutate(out["resume_facts"])
    problems = validate_selection(_resume(), out)
    assert problems, f"expected a rejection for: {expect}"
    assert any(expect in p for p in problems), problems


def test_missing_resume_facts_is_rejected():
    from honestapply.stages.tailor import validate_selection

    assert validate_selection(_resume(), {"name": "x"}) == ["output has no resume_facts mapping"]


def test_preserve_mode_is_default():
    from honestapply.resume.schema import Resume

    r = Resume(name="x")
    assert r.tailoring == "preserve" and not r.selects
    assert _resume().selects


def test_run_tailor_select_mode_end_to_end(add_job, tmp_path, monkeypatch):
    """With a select-mode master on disk the stub (which echoes the YAML back)
    must produce a TAILORED job, a PDF and the kept-facts YAML beside it."""
    import os

    from honestapply.config import PATHS
    from honestapply.db.models import Job, Status
    from honestapply.db.session import session_scope
    from honestapply.stages.tailor import TAILORED_YAML_NAME, run_tailor

    rdir = tmp_path / "resumes"
    rdir.mkdir()
    (rdir / "default.yaml").write_text(yaml.safe_dump(MASTER, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(type(PATHS), "resumes_dir", property(lambda self: rdir))

    jid = add_job(status=Status.SCORED, description="We need a python llm engineer.")
    assert run_tailor() == 1
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == Status.TAILORED, j.status_reason
        assert j.tailored_resume_path and os.path.exists(j.tailored_resume_path)
        kept = yaml.safe_load(
            open(os.path.join(os.path.dirname(j.tailored_resume_path), TAILORED_YAML_NAME))
        )
        assert kept["tailoring"] == "select"
        assert kept["resume_facts"]["experience"][0]["company"] == "Acme"
