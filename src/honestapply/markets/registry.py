"""Market lookup by code or country, with a safe international default."""

from __future__ import annotations

from honestapply.markets.base import Market
from honestapply.markets.de import DE_DE

# The fallback: no photo, no salutation field, ISO dates, plain currency. Used
# for anywhere honestapply doesn't yet ship specific conventions, so an unknown
# market degrades to a neutral international application rather than a German one.
INTERNATIONAL = Market(code="en", name="International", language="en")

_BY_CODE: dict[str, Market] = {m.code: m for m in (DE_DE, INTERNATIONAL)}

# Country names/synonyms → market code, matched case-insensitively on substrings.
_COUNTRY_TO_CODE: dict[str, str] = {
    "germany": "de-DE",
    "deutschland": "de-DE",
    "de": "de-DE",
}


def get_market(code: str | None) -> Market:
    """Look up a market by its code; unknown or None yields the international one."""
    return _BY_CODE.get((code or "").strip(), INTERNATIONAL)


def market_for_country(country: str | None) -> Market:
    """Best-effort market for a free-text country name (e.g. config's `country`)."""
    key = (country or "").strip().lower()
    return get_market(_COUNTRY_TO_CODE.get(key))


def all_markets() -> list[Market]:
    return list(_BY_CODE.values())
