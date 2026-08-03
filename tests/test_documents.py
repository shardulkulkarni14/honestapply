"""Tests for supporting-document bundling."""

from __future__ import annotations

from pathlib import Path


def _blank_pdf(path: Path, pages: int = 1) -> Path:
    try:
        from pypdf import PdfWriter
    except ImportError:
        from PyPDF2 import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    with open(path, "wb") as fh:
        w.write(fh)
    return path


def test_build_bundle_empty_returns_none(tmp_path):
    from honestapply.documents import build_bundle

    assert build_bundle(tmp_path / "out.pdf", paths=[]) is None


def test_build_bundle_merges_pdfs(tmp_path):
    from honestapply.documents import build_bundle

    a = _blank_pdf(tmp_path / "a.pdf", pages=2)
    b = _blank_pdf(tmp_path / "b.pdf", pages=1)
    out = build_bundle(tmp_path / "merged.pdf", paths=[a, b])
    assert out is not None and out.exists()

    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader
    assert len(PdfReader(str(out)).pages) == 3


def test_build_bundle_skips_missing(tmp_path):
    from honestapply.documents import build_bundle

    a = _blank_pdf(tmp_path / "a.pdf", pages=1)
    out = build_bundle(tmp_path / "merged.pdf", paths=[a, tmp_path / "nope.pdf"])
    assert out is not None
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader
    assert len(PdfReader(str(out)).pages) == 1
