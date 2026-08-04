"""The provenance attestation: every immutable fact verified in the rendered PDF."""

from __future__ import annotations

from pathlib import Path

import pytest

from honestapply.provenance import attest, extract_pdf_text

RESUME = Path("data/resumes/default.yaml")
needs_resume = pytest.mark.skipif(not RESUME.exists(), reason="resume YAML not present")


def _render_pdf(text_lines: list[str], out: Path) -> Path:
    """Render a minimal PDF containing the given lines, via the real renderer path."""
    import weasyprint

    html = "<html><body>" + "".join(f"<p>{line}</p>" for line in text_lines) + "</body></html>"
    weasyprint.HTML(string=html).write_pdf(str(out))
    return out


def test_all_facts_present_verifies(tmp_path):
    facts = ["99.84% faster inference", "Led a team of 5 engineers", "Python, LangGraph"]
    pdf = _render_pdf(["Summary", *facts, "References available"], tmp_path / "r.pdf")

    att = attest(facts, pdf)
    assert att.pdf_extractable is True
    assert att.all_verified is True
    assert att.facts_verified == 3
    assert att.unverified == []
    assert "traceable" in att.as_dict()["summary"]


def test_a_missing_fact_is_flagged(tmp_path):
    facts = ["99.84% faster inference", "A claim the PDF omits"]
    pdf = _render_pdf(["99.84% faster inference only"], tmp_path / "r.pdf")

    att = attest(facts, pdf)
    assert att.all_verified is False
    assert att.facts_verified == 1
    assert att.unverified == ["A claim the PDF omits"]


def test_unreadable_pdf_is_not_extractable(tmp_path):
    missing = tmp_path / "nope.pdf"
    att = attest(["anything"], missing)
    assert att.pdf_extractable is False
    assert att.all_verified is False
    assert "Could not extract" in att.as_dict()["summary"]


def test_extract_returns_none_for_missing_file(tmp_path):
    assert extract_pdf_text(tmp_path / "absent.pdf") is None


def test_provenance_endpoint_on_a_tailored_job(add_job):
    """End-to-end: tailor a job, then the endpoint attests its rendered PDF."""
    from fastapi.testclient import TestClient

    import dashboard.api as api
    from honestapply.db.models import Status

    if not RESUME.exists():
        pytest.skip("resume YAML not present")

    jid = add_job(status=Status.SCORED)
    from honestapply.stages.tailor import run_tailor

    run_tailor()
    client = TestClient(api.app)
    resp = client.get(f"/api/jobs/{jid}/provenance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["facts_total"] > 0
    assert body["pdf_extractable"] is True
    # The immutable-facts guarantee means tailoring cannot drop a fact.
    assert body["all_verified"] is True
