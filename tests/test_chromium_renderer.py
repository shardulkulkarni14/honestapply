"""The OPTIONAL chromium renderer: renders the SAME HTML as WeasyPrint and keeps
ATS text-extraction order. Skips cleanly where no Chromium/Chrome is available, so
CI without a browser stays green. WeasyPrint stays the default — asserted here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from honestapply.config import Settings
from honestapply.provenance import extract_pdf_text
from honestapply.resume.chromium import chromium_available
from honestapply.resume.renderer import render_resume_pdf
from honestapply.resume.schema import load_resume

EXAMPLE = Path("config/resume.example.yaml")

needs_chromium = pytest.mark.skipif(
    not chromium_available(),
    reason="no playwright Chromium or Chrome/Chromium binary available",
)


def test_default_renderer_is_weasyprint():
    """The flag must default to WeasyPrint — the chromium path is opt-in only."""
    assert Settings(_env_file=None).resume_renderer == "weasyprint"


@needs_chromium
def test_chromium_renders_example_resume(tmp_path):
    resume = load_resume(EXAMPLE)
    out = render_resume_pdf(resume, tmp_path / "resume.pdf", renderer="chromium")

    # Non-empty PDF.
    assert out.exists()
    assert out.stat().st_size > 0

    # Text-extractable (the same check an ATS and the provenance attester apply).
    text = extract_pdf_text(out)
    assert text is not None
    assert len(text.strip()) > 0

    # Known facts from the bundled example résumé are present.
    lowered = text.lower()
    for fact in (
        "alex example",
        "backend engineer",
        "acme logistics gmbh",
        "startup labs ug",
        "m.sc. computer science",
        "2m requests/day",
    ):
        assert fact in lowered, f"missing fact in extracted text: {fact!r}"

    # ATS order sanity: the newer role extracts before the older one, headline
    # before experience — i.e. the layout did not scramble reading order.
    assert lowered.find("alex example") < lowered.find("acme logistics gmbh")
    assert lowered.find("acme logistics gmbh") < lowered.find("startup labs ug")
