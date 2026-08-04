"""Turn the no-fabrication guarantee into a visible, per-application attestation.

The immutable-facts validator runs during tailoring and then disappears — its
guarantee is real but invisible, which the positioning research flagged as a
mistake: it is the single most differentiated thing honestapply does, and nothing
in the product surfaces it. This module makes it inspectable after the fact.

Crucially it attests against the *rendered PDF's extracted text*, not the source
YAML. That is the stronger claim and a strictly harder test: it confirms every
immutable fact survives into the bytes an employer's ATS will actually parse. If
the layout scrambles text extraction (the floated-dates failure this project has
hit before), a fact goes missing here even though it is present in the source —
so this doubles as an ATS-parseability check.

The output is deliberately shaped like something an employer could one day
consume: "N factual claims, all traceable to candidate-provided source, M
human-verified, 0 fabricated." No identity is asserted — only that the content is
grounded — which is the white space the identity-verification incumbents leave open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


def _normalise(text: str) -> str:
    """Collapse whitespace and lowercase — the same light normalisation the tailor
    validator uses, so 'present' means the same thing in both places."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def extract_pdf_text(pdf_path: str | Path) -> str | None:
    """Text an ATS would extract from the PDF, or None if it can't be read."""
    path = Path(pdf_path)
    if not path.exists():
        return None
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is in the resume extra
        return None
    try:
        reader = PdfReader(str(path))
        parts: list[str] = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
            # Hyperlink targets live in link annotations, not the text layer — a
            # URL shown as clickable text ("LinkedIn") has its address only here.
            # Include them so a URL fact counts as present when it's a real link,
            # while a text-layer-only ATS reader would still be reflected by the
            # extract_text() portion above.
            for annot in page.get("/Annots", []) or []:
                try:
                    uri = annot.get_object().get("/A", {}).get("/URI")
                    if uri:
                        parts.append(str(uri))
                except Exception:  # noqa: BLE001 - skip malformed annotations
                    continue
        return "\n".join(parts)
    except Exception:  # noqa: BLE001 - a malformed PDF is "not extractable", not a crash
        return None


@dataclass
class Attestation:
    facts_total: int
    facts_verified: int
    unverified: list[str]
    pdf_extractable: bool

    @property
    def all_verified(self) -> bool:
        return self.pdf_extractable and self.facts_total > 0 and not self.unverified

    def as_dict(self) -> dict:
        return {
            "facts_total": self.facts_total,
            "facts_verified": self.facts_verified,
            "unverified": self.unverified,
            "pdf_extractable": self.pdf_extractable,
            "all_verified": self.all_verified,
            # A one-line, employer-legible summary of what was checked.
            "summary": self._summary(),
        }

    def _summary(self) -> str:
        if not self.pdf_extractable:
            return "Could not extract text from the rendered PDF to verify."
        if self.facts_total == 0:
            return "No immutable facts declared to verify."
        if self.all_verified:
            return (
                f"All {self.facts_total} factual claims are traceable to "
                f"candidate-provided source and present in the parsed résumé."
            )
        return (
            f"{self.facts_verified}/{self.facts_total} claims verified in the parsed "
            f"résumé; {len(self.unverified)} not found — review before sending."
        )


def attest(resume_facts: list[str], tailored_pdf_path: str | Path) -> Attestation:
    """Verify each immutable fact is present in the rendered PDF's extracted text."""
    text = extract_pdf_text(tailored_pdf_path)
    if text is None:
        return Attestation(
            facts_total=len(resume_facts), facts_verified=0,
            unverified=list(resume_facts), pdf_extractable=False,
        )
    haystack = _normalise(text)
    unverified = [f for f in resume_facts if _normalise(f) not in haystack]
    return Attestation(
        facts_total=len(resume_facts),
        facts_verified=len(resume_facts) - len(unverified),
        unverified=unverified,
        pdf_extractable=True,
    )


def attest_job(job, resumes_dir: str | Path) -> Attestation | None:
    """Build an attestation for a tailored job, or None if it isn't tailored yet."""
    from honestapply.resume.schema import iter_immutable_strings, load_resume

    if not job.tailored_resume_path or not job.matched_resume_path:
        return None
    base = load_resume(job.matched_resume_path)
    facts = iter_immutable_strings(base.resume_facts)
    return attest(facts, job.tailored_resume_path)
