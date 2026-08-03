"""Resume + cover-letter rendering: structured data -> HTML (Jinja2) -> PDF (WeasyPrint).

ATS-friendliness is the job of the *templates* (single column, real selectable
text, standard headings, no layout tables). This module only assembles a
well-defined context and drives the render.

TEMPLATE CONTEXT CONTRACT (what `modern.html` receives) -- keep in sync with the
template authored alongside it:

    contact:      {name, title, location, email, phone, linkedin, github, website}
    summary:      str                       # possibly tailored
    skills:       dict[str, str]            # category -> comma-joined items
    experience:   [{company, title, dates, location, bullets: [str]}]
    education:    [{degree, institution, location, dates, details}]
    projects:     [{name, description, bullets: [str]}]
    awards:       [str]
    languages:    [str]
    certifications:[str]
    accent:       str                       # hex color for headings/rules
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from honestapply.resume.schema import Resume, ResumeFacts

TEMPLATES_DIR = Path(__file__).parent / "templates"
DEFAULT_TEMPLATE = "modern.html"
DEFAULT_ACCENT = "#1a3c5e"


def _mdbold(value: Any) -> "Markup":
    """Escape HTML, then render **text** as <strong>text</strong>. Safe: any
    real markup in the source is escaped first, so only our own ** markers bold.
    Plain strings (no **) pass through unchanged — backward compatible."""
    import re as _re

    from markupsafe import Markup, escape

    s = str(escape(value))
    s = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return Markup(s)


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["mdbold"] = _mdbold
    return env


def _skills_to_strings(skills: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (skills or {}).items():
        if isinstance(v, (list, tuple)):
            out[k] = ", ".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


def build_context(
    facts: ResumeFacts,
    *,
    summary: str,
    experience_order: list[int] | None = None,
    accent: str = DEFAULT_ACCENT,
) -> dict[str, Any]:
    """Assemble the template context from immutable facts + a chosen summary.

    `experience_order` optionally reorders experience entries (tailoring may
    surface the most JD-relevant roles first) without mutating any fact.
    """
    experience = [e.model_dump() for e in facts.experience]
    if experience_order:
        valid = [i for i in experience_order if 0 <= i < len(experience)]
        valid += [i for i in range(len(experience)) if i not in valid]
        experience = [experience[i] for i in valid]

    return {
        "contact": facts.contact.model_dump(),
        "summary": summary,
        "skills": _skills_to_strings(facts.skills),
        "experience": experience,
        "education": [e.model_dump() for e in facts.education],
        "projects": [p.model_dump() for p in facts.projects],
        "awards": list(facts.awards),
        "languages": list(facts.languages),
        "certifications": list(facts.certifications),
        "accent": accent,
    }


def render_resume_html(
    resume: Resume,
    *,
    summary: str | None = None,
    experience_order: list[int] | None = None,
    template: str = DEFAULT_TEMPLATE,
    accent: str = DEFAULT_ACCENT,
) -> str:
    summary = summary if summary is not None else (
        resume.summary_variants[0] if resume.summary_variants else ""
    )
    ctx = build_context(
        resume.resume_facts, summary=summary, experience_order=experience_order, accent=accent
    )
    return _env().get_template(template).render(**ctx)


def render_resume_pdf(
    resume: Resume,
    output_path: str | Path,
    *,
    summary: str | None = None,
    experience_order: list[int] | None = None,
    template: str = DEFAULT_TEMPLATE,
    accent: str = DEFAULT_ACCENT,
) -> Path:
    import weasyprint  # local import so the DYLD fix in __init__ has run

    html = render_resume_html(
        resume, summary=summary, experience_order=experience_order,
        template=template, accent=accent,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    weasyprint.HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(str(output_path))
    return output_path


# ---------------------------------------------------------------------------
# Cover letter (kept decoupled from the resume template; ATS-plain layout)
# ---------------------------------------------------------------------------
_COVER_CSS = """
@page { size: A4; margin: 2.2cm 2cm; }
body { font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 11pt;
       line-height: 1.5; color: #1a1a1a; }
.header { margin-bottom: 1.2em; }
.header .name { font-size: 16pt; font-weight: 700; }
.header .contact { font-size: 9.5pt; color: #444; margin-top: 2px; }
.meta { margin: 1.2em 0; font-size: 10pt; color: #333; }
p { margin: 0 0 0.9em 0; }
.sign { margin-top: 1.4em; }
"""


def render_cover_letter_pdf(
    body: str,
    contact: dict[str, Any],
    output_path: str | Path,
    *,
    company: str = "",
    role: str = "",
) -> Path:
    import html as _html

    import weasyprint

    paras = "".join(
        f"<p>{_html.escape(p.strip())}</p>" for p in body.split("\n\n") if p.strip()
    )
    name = _html.escape(str(contact.get("name", "")))
    cbits = [contact.get(k, "") for k in ("location", "email", "phone")]
    contact_line = _html.escape(" · ".join(b for b in cbits if b))
    today = _dt.date.today().strftime("%d %B %Y")
    meta = _html.escape(" — ".join(b for b in (company, role) if b))

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{_COVER_CSS}</style></head><body>
<div class="header"><div class="name">{name}</div>
<div class="contact">{contact_line}</div></div>
<div class="meta">{today}{'<br>' + meta if meta else ''}</div>
{paras}
</body></html>"""

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    weasyprint.HTML(string=doc).write_pdf(str(output_path))
    return output_path
