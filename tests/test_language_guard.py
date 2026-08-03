"""The CEFR over-claim guard must catch English + German over-claims and respect
the profile's canonical level cap."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _resume(*languages):
    return SimpleNamespace(resume_facts=SimpleNamespace(languages=list(languages)))


@pytest.fixture
def german_a2(monkeypatch):
    """Profile says German A2, English C2 (the canonical cap)."""
    import honestapply.config as cfg

    prof = SimpleNamespace(language_levels={"german": "A2", "english": "C2"})
    monkeypatch.setattr(cfg, "load_profile", lambda *a, **k: prof)
    return prof


def test_flags_native_german(german_a2):
    from honestapply.stages.cover_letter import validate_cover_letter

    issues = validate_cover_letter("I am a native German speaker.", _resume("German (A2)"))
    assert issues and "german" in issues[0].lower()


def test_flags_german_phrasing(german_a2):
    from honestapply.stages.cover_letter import validate_cover_letter

    # "verhandlungssicheres Deutsch" implies ~C1 — over A2
    issues = validate_cover_letter("Ich bringe verhandlungssicheres Deutsch mit.", _resume("German (A2)"))
    assert issues


def test_allows_truthful_english(german_a2):
    from honestapply.stages.cover_letter import validate_cover_letter

    issues = validate_cover_letter("I am fluent in English.", _resume("English (C2)", "German (A2)"))
    assert issues == []


def test_profile_cap_overrides_overstated_resume(german_a2):
    from honestapply.stages.cover_letter import validate_cover_letter

    # résumé over-lists German as C1, but profile caps it at A2 → fluent claim flagged
    issues = validate_cover_letter("I have fluent German.", _resume("German (C1)"))
    assert issues
