"""Work-auth geo pre-filter: skip only roles in countries the profile can't work in."""

from __future__ import annotations

# An EU-only profile: authorized in the EU, not in the US/UK (needs sponsorship).
EU_ONLY = {"eu_authorized": True, "us_work_authorization": False, "right_to_work_uk": False}


def test_country_from_location():
    from honestapply.work_authorization import country_from_location

    assert country_from_location("Bucharest, Romania") == "Romania"
    assert country_from_location("Munich, Germany") == "Germany"
    assert country_from_location("Remote") == ""        # no country
    assert country_from_location("") == ""
    assert country_from_location("London, United Kingdom") == "United Kingdom"


def test_is_confident_unauthorized_only_on_confident_no():
    from honestapply.work_authorization import is_confident_unauthorized

    # Confident NO -> filter
    assert is_confident_unauthorized(EU_ONLY, "San Francisco, United States") is True
    assert is_confident_unauthorized(EU_ONLY, "London, United Kingdom") is True
    # Authorized / unknown / remote -> never filter
    assert is_confident_unauthorized(EU_ONLY, "Munich, Germany") is False
    assert is_confident_unauthorized(EU_ONLY, "Remote") is False
    assert is_confident_unauthorized(EU_ONLY, "Tokyo, Japan") is False  # bucket unknown
    assert is_confident_unauthorized({}, "San Francisco, United States") is False  # profile silent


def test_skip_unauthorized_geo_drops_only_confident_no():
    from honestapply.db.models import Job, Status, url_hash
    from honestapply.db.session import session_scope
    from honestapply.stages.tailor import _skip_unauthorized_geo

    def mk(loc):
        u = f"https://x/{loc}"
        return Job(company="Acme", title="AI Engineer", url=u, url_hash=url_hash(u),
                   status=Status.SCORED, location=loc)

    with session_scope() as s:
        us = mk("Austin, United States")
        de = mk("Berlin, Germany")
        remote = mk("Remote")
        s.add_all([us, de, remote])
        s.flush()

        kept = _skip_unauthorized_geo(s, [us.id, de.id, remote.id], EU_ONLY)
        s.flush()

        assert kept == [de.id, remote.id]  # only the US role dropped
        assert s.get(Job, us.id).status == Status.SKIPPED_WORK_AUTH
        assert s.get(Job, de.id).status == Status.SCORED
        assert s.get(Job, remote.id).status == Status.SCORED
