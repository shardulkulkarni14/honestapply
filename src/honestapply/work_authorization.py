"""Truthful answers to the work-authorisation questions on application forms.

This is the honesty rule applied to the highest-stakes field on the form. "Are
you authorised to work here?" and "do you need sponsorship?" must be answered
from what the profile actually establishes, per target country — never guessed,
because a wrong answer here is both a lie and, often, an automatic rejection.

The design mirrors the résumé fact validator's posture: when the profile does not
*establish* the answer for a country, this returns ``confident=False`` and the
caller routes the job to ``needs_human`` rather than emitting a plausible guess.
It is the piece that makes honestapply usable by exactly the people the German
market is filling up with — non-EU skilled workers on Blue Cards and
Chancenkarten, for whom the honest answer differs by country and getting it
wrong is costly.
"""

from __future__ import annotations

from dataclasses import dataclass

# Country buckets we can reason about. Anything else → not confident.
_EU_EEA = {
    "germany", "deutschland", "de", "austria", "netherlands", "france", "spain",
    "italy", "belgium", "ireland", "portugal", "sweden", "denmark", "finland",
    "poland", "eu", "europe", "eea",
}
_US = {"united states", "usa", "us", "u.s.", "america"}
_UK = {"united kingdom", "uk", "u.k.", "england", "scotland", "wales", "britain"}


@dataclass(frozen=True)
class WorkAuthAnswer:
    authorized: bool | None
    needs_sponsorship: bool | None
    statement: str
    confident: bool  # False → the profile doesn't establish this; route to human

    def as_dict(self) -> dict:
        return {
            "authorized": self.authorized,
            "needs_sponsorship": self.needs_sponsorship,
            "statement": self.statement,
            "confident": self.confident,
        }


def _unsure(country: str) -> WorkAuthAnswer:
    return WorkAuthAnswer(
        authorized=None,
        needs_sponsorship=None,
        statement="",
        confident=False,
    )


def answer(work_auth: dict, target_country: str) -> WorkAuthAnswer:
    """A truthful work-authorisation answer for applying in ``target_country``.

    ``work_auth`` is ``profile["work_authorization"]``. Recognised keys:
    ``eu_authorized``, ``us_work_authorization``, ``right_to_work_uk``,
    ``visa_type``. Missing keys are treated as *unknown*, never as False, so the
    result is "not confident" rather than a confident "no".
    """
    country = (target_country or "").strip().lower()
    wa = work_auth or {}

    if country in _EU_EEA:
        eu = wa.get("eu_authorized")
        if eu is True:
            visa = wa.get("visa_type", "")
            note = f" ({visa})" if visa and "TODO" not in str(visa) else ""
            return WorkAuthAnswer(
                authorized=True,
                needs_sponsorship=False,
                statement=f"Authorised to work in the EU/EEA{note}; no sponsorship required.",
                confident=True,
            )
        if eu is False:
            return WorkAuthAnswer(
                authorized=False,
                needs_sponsorship=True,
                statement="Not currently authorised to work in the EU/EEA; would require sponsorship.",
                confident=True,
            )
        return _unsure(country)

    if country in _US:
        us = wa.get("us_work_authorization")
        if us is True:
            return WorkAuthAnswer(
                authorized=True,
                needs_sponsorship=False,
                statement="Authorised to work in the US; no sponsorship required.",
                confident=True,
            )
        if us is False:
            return WorkAuthAnswer(
                authorized=False,
                needs_sponsorship=True,
                statement="Not authorised to work in the US; would require visa sponsorship.",
                confident=True,
            )
        return _unsure(country)

    if country in _UK:
        uk = wa.get("right_to_work_uk")
        if uk is True:
            return WorkAuthAnswer(
                authorized=True,
                needs_sponsorship=False,
                statement="Hold the right to work in the UK; no sponsorship required.",
                confident=True,
            )
        if uk is False:
            return WorkAuthAnswer(
                authorized=False,
                needs_sponsorship=True,
                statement="Do not hold the right to work in the UK; would require sponsorship.",
                confident=True,
            )
        return _unsure(country)

    # Unknown country: never assert authorisation we can't establish.
    return _unsure(country)
