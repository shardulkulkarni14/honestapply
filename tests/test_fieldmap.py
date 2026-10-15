"""The deterministic fill plan fills only truthful, non-placeholder fields.

These are pure unit tests — no browser, no `claude` subprocess, no network.
"""

from __future__ import annotations

import json
import re

import yaml

from honestapply.ats.fieldmap import build_field_plan, render_fill_plan
from honestapply.config import Profile

# Separate first/last inputs (Greenhouse/Ashby-style).
GREENHOUSE = {
    "first_name": 'input[name="first"]',
    "last_name": 'input[name="last"]',
    "email": 'input[type="email"]',
    "phone": 'input[type="tel"]',
    "linkedin_url": 'input[name="linkedin"]',
    "resume_upload": 'input[type="file"]',
    "submit": 'button[type="submit"]',
    "notes": "structural hint for the agent",
}
# Single combined name input with an empty last_name (Lever-style).
LEVER = {
    "first_name": 'input[name="name"]',
    "last_name": "",
    "full_name": 'input[name="name"]',
    "email": 'input[type="email"]',
    "submit": "button",
}


def _profile(**over):
    base = dict(
        legal_name={"first": "Shardul", "last": "Kulkarni"},
        email="s@example.com",
        phone="+49 100 000",
        linkedin_url="https://linkedin.com/in/x",
        github_url="https://github.com/x",
        portfolio_url="",
        address={"city": "Munich", "country": "Germany", "postal_code": "80331"},
    )
    base.update(over)
    return Profile(**base)


def test_greenhouse_fills_standard_contact_fields():
    plan = build_field_plan("greenhouse", _profile(), {}, GREENHOUSE)
    by = {a.field_key: a for a in plan.actions}
    assert by["first_name"].value == "Shardul"
    assert by["last_name"].value == "Kulkarni"
    assert by["email"].value == "s@example.com"
    assert by["phone"].value == "+49 100 000"
    assert by["linkedin_url"].value == "https://linkedin.com/in/x"
    # uploads / submit / notes are never deterministically filled
    assert "resume_upload" not in by and "submit" not in by and "notes" not in by


def test_lever_single_name_field_gets_full_name_not_first_only():
    plan = build_field_plan("lever", _profile(), {}, LEVER)
    by = {a.field_key: a for a in plan.actions}
    assert "full_name" in by and by["full_name"].value == "Shardul Kulkarni"
    assert "first_name" not in by  # would otherwise type only "Shardul"
    # never two actions on the same input
    selectors = [a.selector for a in plan.actions]
    assert len(selectors) == len(set(selectors))


def test_placeholder_values_are_not_filled():
    p = _profile(address={"city": "Munich", "country": "Germany", "postal_code": "TODO_FILL_IN"})
    plan = build_field_plan("workday", p, {}, {"city": "input[c]", "postal_code": "input[p]"})
    by = {a.field_key for a in plan.actions}
    assert "city" in by
    assert "postal_code" not in by
    assert "postal_code" in plan.deferred


def test_empty_values_are_deferred():
    plan = build_field_plan("greenhouse", _profile(portfolio_url=""), {}, {"portfolio_url": "input[p]"})
    assert not any(a.field_key == "portfolio_url" for a in plan.actions)
    assert "portfolio_url" in plan.deferred


def test_every_planned_value_is_traceable_to_profile_or_answers():
    """No-fabrication property: every token of every planned value came from the
    profile or answers — the plan can never introduce a string the user didn't give."""
    p = _profile()
    answers = {"current_employment": {"current_company": "MAindTec GmbH"}}
    selectors = {
        "first_name": "a",
        "last_name": "b",
        "email": "c",
        "phone": "d",
        "linkedin_url": "e",
        "city": "f",
        "country": "g",
        "org": "h",
    }
    plan = build_field_plan("greenhouse", p, answers, selectors)
    haystack = json.dumps(p.model_dump(), ensure_ascii=False) + yaml.safe_dump(answers)
    assert plan.actions
    for a in plan.actions:
        for token in re.split(r"[\s,]+", a.value):
            if token:
                assert token in haystack, f"{token!r} (in {a.value!r}) not traceable to profile/answers"


def test_unknown_fields_defer_to_the_agent():
    plan = build_field_plan("generic", _profile(), {}, {"weird_custom_q": "input[x]", "email": "input[e]"})
    assert any(a.field_key == "email" for a in plan.actions)
    assert "weird_custom_q" in plan.deferred


def test_submit_is_never_in_the_plan():
    plan = build_field_plan("greenhouse", _profile(), {}, {"submit": "button", "email": "input[e]"})
    assert all(a.field_key != "submit" for a in plan.actions)


def test_phone_india_rule_selects_the_right_number():
    p = _profile(phone="+49 1", phone_india="+91 9")
    de = build_field_plan("greenhouse", p, {}, {"phone": "input[p]"}, target_country="germany")
    inn = build_field_plan("greenhouse", p, {}, {"phone": "input[p]"}, target_country="india")
    assert next(a for a in de.actions if a.field_key == "phone").value == "+49 1"
    assert next(a for a in inn.actions if a.field_key == "phone").value == "+91 9"


def test_render_fill_plan_is_empty_message_when_no_actions():
    plan = build_field_plan("generic", Profile(), {}, {})
    assert not plan.actions
    assert "No fast-path fields" in render_fill_plan(plan)
