"""Supporting-document handling: merge transcripts / reference letters / work
references (Arbeitszeugnis) into a single PDF for forms that demand one upload.

Several ATS forms (e.g. SmartRecruiters, some Lever boards) require "University
Transcripts & Reference Letters" as a *single* PDF. The applicant's documents
live in scattered files; `build_bundle()` merges the configured set into one PDF.

Paths come from the profile's optional `supporting_documents` list (absolute or
relative to the repo root). Missing files are skipped with a warning rather than
failing the whole bundle — never fabricate a document that doesn't exist.
"""

from __future__ import annotations

from pathlib import Path

from honestapply.config import PATHS, load_profile
from honestapply.logging_setup import get_logger

log = get_logger(__name__)


def supporting_document_paths() -> list[Path]:
    """Return the configured supporting-document paths that actually exist on disk."""
    profile = load_profile()
    raw = getattr(profile, "supporting_documents", None) or []
    resolved: list[Path] = []
    for entry in raw:
        p = Path(entry).expanduser()
        if not p.is_absolute():
            p = (PATHS.root / p).resolve() if hasattr(PATHS, "root") else p.resolve()
        if p.exists():
            resolved.append(p)
        else:
            log.warning("documents.missing", path=str(p))
    return resolved


def build_bundle(out_path: str | Path, paths: list[Path] | None = None) -> Path | None:
    """Merge the given (or configured) PDFs into a single PDF at *out_path*.

    Returns the output Path, or None if there were no usable source documents.
    """
    docs = paths if paths is not None else supporting_document_paths()
    docs = [p for p in docs if Path(p).suffix.lower() == ".pdf" and Path(p).exists()]
    if not docs:
        log.warning("documents.bundle_empty", reason="no usable PDF source documents")
        return None

    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:  # pragma: no cover - fallback for older envs
        from PyPDF2 import PdfReader, PdfWriter

    writer = PdfWriter()
    pages = 0
    for doc in docs:
        try:
            reader = PdfReader(str(doc))
        except Exception as exc:  # noqa: BLE001 - skip unreadable, don't fail bundle
            log.warning("documents.unreadable", path=str(doc), error=str(exc))
            continue
        for page in reader.pages:
            writer.add_page(page)
            pages += 1

    if pages == 0:
        return None

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        writer.write(fh)
    log.info("documents.bundle_built", out=str(out), sources=len(docs), pages=pages)
    return out
