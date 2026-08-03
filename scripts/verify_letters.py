"""Pre-apply gate: refuse to submit a cover letter containing model commentary.

`cover_letter.py` strips model preamble before rendering, but that guard runs at
generation time. This checks the artifact that actually gets uploaded — the
rendered PDF — so a regression in generation, a stale PDF from an older run, or
a provider quirk cannot put commentary in front of an employer.

A real run once produced a letter opening with "Every string in `resume_facts`
is immutable and the tailor validator substring-checks it ... Here's the draft:".
That is what this catches.

Contaminated jobs are routed to needs_human (never silently dropped, never
submitted). Clean jobs are left alone.

    python scripts/verify_letters.py            # check + route offenders
    python scripts/verify_letters.py --report   # check only, change nothing

Exit code is 0 even when offenders are found (they are handled by routing);
it is non-zero only on an unreadable PDF, so a cycle does not die on one bad file.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import select

from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope

# Phrases that only appear when the model narrates instead of writing the letter.
_CONTAMINATION = [
    "resume_facts",
    "substring-check",
    "substring check",
    "tailor validator",
    "ground truth",
    "here's the draft",
    "here is the draft",
    "here's the cover letter",
    "here is the cover letter",
    "let me know if",
    "hope this helps",
    "as requested",
]

# A letter this short almost certainly lost its body to an over-eager strip.
_MIN_WORDS = 120

# The phrase blocklist above only catches commentary we have already seen. The
# stronger, general check is that a cover letter must OPEN like a letter: a
# leaked preamble pushes the salutation down the page. This caught a draft that
# began "I'll draft I need to reason: candidate summary + JD + honesty
# constraints…" and matched no blocklisted phrase.
# Requiring a salutation is WRONG: plenty of good letters open with a hook
# ("Forward Deployed Engineering here is a natural fit for how I work…").
# What actually distinguishes a leak is the model talking about the TASK of
# writing rather than addressing the employer. Match that instead.
_TASK_META = re.compile(
    r"("
    r"i'?ll (?:draft|write|produce)|i will (?:draft|write|produce)|"
    r"i need to reason|let me (?:draft|write)|"
    r"honesty constraints?|letter body|candidate summary|resume_facts|"
    r"per the (?:instructions|prompt)|as instructed|following the constraints|"
    r"only the body|the job description above|the jd\b|"
    r"substring-check|tailor validator|ground truth"
    r")",
    re.IGNORECASE,
)


def _body_after_header(text: str) -> str:
    """Strip the rendered letterhead (name, contact line, date, role line).

    render_cover_letter_pdf prints those above the body, so the salutation is
    never literally the first line of the extracted PDF text.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Drop the first few header lines, then return what's left.
    return "\n".join(lines[4:]) if len(lines) > 4 else "\n".join(lines)


def _pdf_text(path: Path) -> str:
    return "".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="check only; do not modify the DB")
    args = ap.parse_args()

    offenders: list[tuple[int, str]] = []
    checked = 0

    with session_scope() as s:
        jobs = s.scalars(select(Job).where(Job.status == Status.COVERED)).all()
        for job in jobs:
            pdf = Path(f"data/outputs/{job.id}/cover_letter.pdf")
            if not pdf.exists():
                offenders.append((job.id, "cover_letter.pdf missing"))
                continue

            try:
                text = _pdf_text(pdf)
            except Exception as exc:  # noqa: BLE001
                offenders.append((job.id, f"unreadable PDF: {exc}"))
                continue

            checked += 1
            lowered = text.lower()
            hits = [m for m in _CONTAMINATION if m in lowered]
            words = len(re.findall(r"\w+", text))

            body = _body_after_header(text)
            if hits:
                offenders.append((job.id, f"model commentary in PDF: {hits}"))
            elif words < _MIN_WORDS:
                offenders.append((job.id, f"letter too short ({words} words)"))
            elif _TASK_META.search(body[:400]):
                # The model narrating the writing task instead of addressing the
                # employer — a leak the fixed phrase list did not anticipate.
                offenders.append(
                    (job.id, f"model narrating the task: {body[:90]!r}")
                )

        if offenders and not args.report:
            for jid, reason in offenders:
                job = s.get(Job, jid)
                if job is not None:
                    job.status = Status.NEEDS_HUMAN
                    job.status_reason = f"cover letter failed pre-apply check: {reason}"

    print(f"verify_letters: checked {checked} covered letter(s)")
    for jid, reason in offenders:
        verb = "would route" if args.report else "routed"
        print(f"  [{jid}] {reason} -> {verb} to needs_human")
    if not offenders:
        print("  all clean")


if __name__ == "__main__":
    main()
