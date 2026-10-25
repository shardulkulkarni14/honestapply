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


def test_level_of_another_language_in_the_same_sentence_is_not_a_claim(german_a2):
    """"English (C1) and German (A2)" is not a German C1 claim: the C1 belongs to
    the nearer language name. Both sides of the pair must stay clean."""
    from honestapply.stages.cover_letter import validate_cover_letter

    r = _resume("English (C1)", "German (A2)")
    assert validate_cover_letter("I bring fluent English (C1) and German (A2).", r) == []
    assert validate_cover_letter(
        "I communicate in English (C1) and German (A2) with clients.", r
    ) == []
    assert validate_cover_letter("German (A2) alongside English at C1 level.", r) == []


def test_level_attached_to_the_weaker_language_is_still_caught(german_a2):
    """Mentioning English nearby must not launder a real German over-claim."""
    from honestapply.stages.cover_letter import validate_cover_letter

    r = _resume("English (C1)", "German (A2)")
    assert validate_cover_letter("Fluent English and C1 German.", r) == [
        "letter claims 'c1' for german, but candidate's level is lower"
    ]
    assert validate_cover_letter("English and fluent German (C1).", r) != []


def test_honest_disclosure_of_the_required_level_is_not_a_claim(german_a2):
    from honestapply.stages.cover_letter import validate_cover_letter

    # Naming the posting's requirement while stating the real level is honest.
    for body in (
        "While your posting asks for fluent German, my German is currently A2.",
        "Your role asks for German at C1; I want to be upfront that my German is A2 today.",
        "Mein Deutsch ist auf Grundkenntnisse-Niveau (A2), nicht fließend.",
    ):
        assert validate_cover_letter(body, _resume("German (A2)")) == []


def test_claim_without_disclosure_is_still_caught(german_a2):
    from honestapply.stages.cover_letter import validate_cover_letter

    for body in (
        "The role requires fluent German, which I bring to the table.",
        "I am a C1 German speaker. My English is C1 and my German is A2 on paper.",
    ):
        assert validate_cover_letter(body, _resume("German (A2)"))
