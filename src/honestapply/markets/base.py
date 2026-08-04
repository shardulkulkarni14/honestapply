"""A *market* is everything about applying for a job that is country-specific.

Job boards are localised everywhere; the documents and the form conventions are
localised almost nowhere — which is the gap honestapply fills. A German
application is not a translated American one: it expects a photo on the
Lebenslauf, a formal Anschreiben, DD.MM.YYYY dates, gross annual salary in euro,
a salutation (Anrede) as a mandatory field, and — for the non-EU skilled workers
arriving on Blue Cards and Chancenkarten — a specific, truthful way to state work
authorisation. None of the US-built tools model any of this.

A Market bundles those conventions behind one interface so the rest of the
pipeline stays locale-agnostic: it asks the market how to format a salary or
which salutations are valid, rather than hard-coding one country's assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Market:
    code: str  # BCP-47-ish, e.g. "de-DE", "en"
    name: str
    language: str  # ISO 639-1, the language application documents are written in
    currency: str = "EUR"
    currency_symbol: str = "€"
    date_format: str = "%Y-%m-%d"

    # Résumé (CV) conventions.
    cv_photo: bool = False  # is a photo expected / acceptable on the CV?
    cv_name: str = "Resume"  # what the document is called locally

    # Application-form vocabulary.
    salutations: tuple[str, ...] = ()  # Anrede options; empty = no such field

    # ATS families that always sit behind an account/login wall in this market
    # and therefore cannot be completed unattended — routed to needs_human up
    # front rather than after burning a browser session.
    account_walled_ats: frozenset[str] = field(default_factory=frozenset)

    def format_salary(self, amount: int, period: str = "year") -> str:
        """A salary string in this market's convention."""
        return f"{self.currency_symbol}{amount:,}/{period}"

    def format_date(self, value: datetime) -> str:
        return value.strftime(self.date_format)
