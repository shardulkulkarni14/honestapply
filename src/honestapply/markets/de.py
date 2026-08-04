"""The German market — the beachhead.

Everything here is a real convention a German application is judged against, and
a place the US-built tools get it wrong. The account-walled ATS set encodes
hard-won knowledge: Haufe Umantis and SAP SuccessFactors both require a portal
account before any form can be touched, so a job on either is routed to a human
immediately instead of failing at submit.
"""

from __future__ import annotations

from honestapply.markets.base import Market


class _GermanMarket(Market):
    def format_salary(self, amount: int, period: str = "year") -> str:
        # German convention: "." groups thousands, gross annual is the norm, and
        # the euro sign trails the amount. e.g. 90000 -> "90.000 € brutto/Jahr".
        grouped = f"{amount:,}".replace(",", ".")
        suffix = "brutto/Jahr" if period == "year" else f"brutto/{period}"
        return f"{grouped} {self.currency_symbol} {suffix}"


DE_DE = _GermanMarket(
    code="de-DE",
    name="Germany",
    language="de",
    currency="EUR",
    currency_symbol="€",
    date_format="%d.%m.%Y",
    cv_photo=True,
    cv_name="Lebenslauf",
    # A German form's Anrede is often a required field; "Divers" is legally
    # recognised, and a "no salutation" option must exist.
    salutations=("Herr", "Frau", "Divers", "Keine Angabe"),
    account_walled_ats=frozenset({"umantis", "successfactors"}),
)
