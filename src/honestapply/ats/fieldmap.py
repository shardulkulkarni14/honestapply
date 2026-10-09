"""Deterministic field→value "fill plan" for the apply stage.

The apply stage is agent-driven: a ``claude`` subprocess fills forms via Playwright
MCP tool calls. The slow, expensive part is the LLM *reasoning* about each field —
reading the label, matching it, deciding a value. But the standard identity and
contact fields every ATS shares (name, email, phone, profile URLs, location,
current employer) are already fully determined by the profile and answers; no
reasoning is needed. This module precomputes those as an explicit list of
``(selector, value)`` actions — the *fill plan* — which the agent executes without
reasoning, leaving only the genuine questions (screening, EEO, work-auth) to the LLM.

NO FABRICATION, by construction: a field is planned only when its value is present,
non-empty, and **not** a placeholder (``TODO*``). Anything that can't be filled
truthfully is left out of the plan and defers to the agent, which routes to
``needs_human`` when it can't answer honestly. The plan can therefore only ever
contain strings the user actually provided — a property the tests assert directly.

Pure functions only: no I/O, no network, no browser.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from honestapply.config import Profile, _is_placeholder


@dataclass(frozen=True)
class FillAction:
    field_key: str  # the ATS selector key, e.g. "email", "first_name"
    selector: str  # the CSS/attribute selector from ats_selectors.yaml
    value: str  # the truthful value to type/select
    source: str  # provenance, e.g. "profile.email"
    kind: str = "text"  # "text" | "select"


@dataclass(frozen=True)
class FieldPlan:
    actions: list[FillAction] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)  # selector keys left to the agent

    def as_dict(self) -> dict:
        return {"actions": [asdict(a) for a in self.actions], "deferred": list(self.deferred)}


# Selector keys the agent always owns — uploads, free text, submit, and the
# human-readable notes. Never part of a deterministic fill.
_NON_FIELD_KEYS = frozenset(
    {
        "resume_upload",
        "cover_letter_upload",
        "cover_letter_text",
        "submit",
        "notes",
        "_notes",
        "how_did_you_hear",
    }
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def build_field_plan(
    ats_type: str,
    profile: Profile,
    answers: dict[str, Any],
    ats_selectors: dict[str, Any],
    target_country: str = "",
) -> FieldPlan:
    """Precompute the truthful, placeholder-free fields fillable without the LLM.

    A field is emitted only when it has a non-empty selector in ``ats_selectors``,
    a non-empty value in the profile/answers, and that value is not a placeholder.
    Everything else is deferred to the agent (its Step 4–8 behavior, including the
    ``needs_human`` fallback). ``ats_type`` is informational; the selectors passed
    in are already the resolved per-ATS block.
    """
    p = profile.model_dump()
    name = p.get("legal_name") or {}
    addr = p.get("address") or {}
    answers = answers if isinstance(answers, dict) else {}
    pinfo = answers.get("personal_info") or {}
    employment = answers.get("current_employment") or {}

    first = _clean(name.get("first")) or _clean(pinfo.get("first_name"))
    last = _clean(name.get("last")) or _clean(pinfo.get("last_name"))
    full = _clean(f"{first} {last}")

    # Phone: the India market uses phone_india when present; otherwise the default.
    phone_india = _clean(p.get("phone_india"))
    if "india" in _clean(target_country).lower() and phone_india:
        phone_val, phone_src = phone_india, "profile.phone_india"
    else:
        phone_val, phone_src = _clean(p.get("phone")) or _clean(pinfo.get("phone")), "profile.phone"

    website = _clean(p.get("portfolio_url")) or _clean(p.get("github_url"))
    website_src = "profile.portfolio_url" if _clean(p.get("portfolio_url")) else "profile.github_url"
    location = _clean(pinfo.get("location")) or _clean(
        ", ".join(x for x in (_clean(addr.get("city")), _clean(addr.get("country"))) if x)
    )

    # Single-field candidates (name is special-cased below).
    candidates: dict[str, tuple[str, str]] = {
        "email": (_clean(p.get("email")) or _clean(pinfo.get("email")), "profile.email"),
        "phone": (phone_val, phone_src),
        "linkedin_url": (_clean(p.get("linkedin_url")), "profile.linkedin_url"),
        "github_url": (_clean(p.get("github_url")), "profile.github_url"),
        "portfolio_url": (_clean(p.get("portfolio_url")), "profile.portfolio_url"),
        "website_url": (website, website_src),
        "address_line1": (_clean(addr.get("line1")), "profile.address.line1"),
        "city": (_clean(addr.get("city")), "profile.address.city"),
        "state": (_clean(addr.get("state")), "profile.address.state"),
        "postal_code": (_clean(addr.get("postal_code")), "profile.address.postal_code"),
        "country": (_clean(addr.get("country")), "profile.address.country"),
        "location": (location, "profile.address"),
        "org": (_clean(employment.get("current_company")), "answers.current_employment.current_company"),
    }

    actions: list[FillAction] = []
    used_selectors: set[str] = set()
    handled: set[str] = set()  # name keys resolved by the single-field branch

    def _sel(key: str) -> str:
        return _clean(ats_selectors.get(key))

    def _emit(key: str, value: str, source: str) -> None:
        selector = _sel(key)
        kind = "select" if selector.lower().startswith("select") else "text"
        actions.append(FillAction(field_key=key, selector=selector, value=value, source=source, kind=kind))
        used_selectors.add(selector)

    # Name: a single-name ATS (full_name/name) takes the full name; otherwise fill
    # first/last separately. This avoids typing only the first name into a combined
    # name field (e.g. Lever's single input).
    full_key = "full_name" if _sel("full_name") else ("name" if _sel("name") else "")
    if full_key and full and not _is_placeholder(full):
        _emit(full_key, full, "profile.legal_name")
        handled |= {"first_name", "last_name", "full_name", "name"}
    else:
        if _sel("first_name") and first and not _is_placeholder(first):
            _emit("first_name", first, "profile.legal_name.first")
        if _sel("last_name") and last and not _is_placeholder(last):
            _emit("last_name", last, "profile.legal_name.last")

    for key, (value, source) in candidates.items():
        selector = _sel(key)
        if not selector or not value or _is_placeholder(value):
            continue
        if selector in used_selectors:  # don't double-fill the same input
            continue
        _emit(key, value, source)

    emitted = {a.field_key for a in actions}
    deferred = [
        key
        for key, sel in ats_selectors.items()
        if key not in _NON_FIELD_KEYS
        and key not in {"full_name", "name"}
        and _clean(sel)
        and key not in emitted
        and key not in handled
    ]
    return FieldPlan(actions=actions, deferred=sorted(set(deferred)))


def render_fill_plan(plan: FieldPlan) -> str:
    """The fill plan as the Markdown block injected into the apply prompt."""
    if not plan.actions:
        return "_No fast-path fields for this form — fill every field per Steps 4–8._"
    lines = [
        f'- **{a.field_key}** — selector `{a.selector}` → set to "{a.value}"  _(from {a.source})_'
        for a in plan.actions
    ]
    out = "\n".join(lines)
    if plan.deferred:
        out += (
            "\n\nFields NOT in the plan (handle per Steps 4–8, including the "
            "needs_human fallback): " + ", ".join(plan.deferred)
        )
    return out
