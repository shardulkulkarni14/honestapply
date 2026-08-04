"""Market conventions, work-authorisation honesty, and account-walled routing.

The work-authorisation tests are the important ones: they pin the guarantee that
honestapply never asserts a work status the profile does not establish — the
no-fabrication rule applied to the field where a wrong answer is both a lie and
an auto-reject.
"""

from __future__ import annotations

from datetime import datetime

from honestapply import work_authorization as wa
from honestapply.ats.detect import detect_ats, is_account_walled
from honestapply.markets.registry import get_market, market_for_country


# --- market conventions -----------------------------------------------------
def test_german_market_conventions():
    de = get_market("de-DE")
    assert de.cv_photo is True
    assert de.cv_name == "Lebenslauf"
    assert "Divers" in de.salutations
    assert de.format_salary(90000) == "90.000 € brutto/Jahr"
    assert de.format_date(datetime(2026, 3, 9)) == "09.03.2026"


def test_international_market_is_the_neutral_default():
    intl = get_market("en")
    assert intl.cv_photo is False
    assert intl.salutations == ()
    assert get_market("zz-ZZ").code == "en"  # unknown → international
    assert get_market(None).code == "en"


def test_market_for_country():
    assert market_for_country("Germany").code == "de-DE"
    assert market_for_country("deutschland").code == "de-DE"
    assert market_for_country("Narnia").code == "en"


# --- account-walled ATS routing ---------------------------------------------
def test_account_walled_detection():
    assert detect_ats("https://recruitingapp-1234.umantis.com/Vacancies/1/x") == "umantis"
    assert detect_ats("https://career55.sapsf.eu/careers?company=x") == "successfactors"
    assert is_account_walled("umantis") is True
    assert is_account_walled("successfactors") is True
    assert is_account_walled("greenhouse") is False


# --- work-authorisation honesty ---------------------------------------------
def test_eu_authorized_answers_germany_truthfully():
    a = wa.answer({"eu_authorized": True, "visa_type": "EU Blue Card"}, "Germany")
    assert a.confident is True
    assert a.authorized is True
    assert a.needs_sponsorship is False
    assert "Blue Card" in a.statement


def test_non_us_authorized_needs_sponsorship_for_us_role():
    a = wa.answer({"eu_authorized": True, "us_work_authorization": False}, "United States")
    assert a.confident is True
    assert a.authorized is False
    assert a.needs_sponsorship is True


def test_unknown_authorization_is_not_guessed():
    """ACCEPTANCE: a missing key must yield not-confident, never a confident 'no'.

    This is the whole point — the caller routes not-confident to needs_human
    instead of asserting something the profile doesn't establish.
    """
    a = wa.answer({}, "Germany")
    assert a.confident is False
    assert a.authorized is None
    assert a.needs_sponsorship is None
    assert a.statement == ""


def test_unknown_country_is_never_asserted():
    a = wa.answer({"eu_authorized": True}, "Japan")
    assert a.confident is False


def test_market_setting_resolves_on_settings():
    from honestapply.config import Settings

    assert Settings(honestapply_market="de-DE").market.code == "de-DE"
    assert Settings(honestapply_market="en").market.code == "en"
