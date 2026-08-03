"""Generic / unknown ATS helper.

Used when detect_ats() returns "generic" — i.e. a custom careers page or an
ATS we don't have specific selectors for.

Strategy for unknown ATS forms:
- Rely heavily on Playwright's getByLabel(), getByRole(), and getByPlaceholder()
  rather than exact CSS selectors.
- Text-match button labels: "Apply", "Apply Now", "Apply for this job", "Submit",
  "Send Application", "Complete Application".
- Take a screenshot early so Claude can visually identify form structure.
- For file uploads, look for any <input type="file"> or a button/link labelled
  "Upload", "Attach", "Add resume".
- If a required field cannot be mapped from the profile/answers, mark needs_human
  rather than leaving it blank.
"""

from __future__ import annotations

from honestapply.config import load_ats_selectors

_FALLBACK: dict = {
    "first_name": 'input[name*="first"], input[id*="first_name"], input[placeholder*="First name" i]',
    "last_name": 'input[name*="last"], input[id*="last_name"], input[placeholder*="Last name" i]',
    "email": 'input[type="email"], input[name*="email"], input[id*="email"]',
    "phone": 'input[type="tel"], input[name*="phone"], input[id*="phone"]',
    "resume_upload": 'input[type="file"][accept*="pdf"], input[type="file"][name*="resume"], input[type="file"][id*="resume"]',
    "cover_letter_upload": 'input[type="file"][name*="cover"], input[type="file"][id*="cover"]',
    "submit": 'button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Apply")',
}


def field_hints() -> dict:
    """Return generic selector hints from config, falling back to hardcoded values."""
    selectors = load_ats_selectors()
    return selectors.get("generic", _FALLBACK)
