"""Feed adapters for the ATSs German employers use (Personio, Recruitee,
Workable, Teamtailor) parse their public payloads offline, and URL detection
knows the new hosts plus the Arbeitsagentur source-only page."""

from __future__ import annotations

from honestapply.config import Employer

PERSONIO_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>123</id>
    <office>M\xc3\xbcnchen</office>
    <additionalOffices></additionalOffices>
    <department>Engineering</department>
    <name>AI Engineer (m/w/d)</name>
    <employmentType>permanent</employmentType>
    <seniority>experienced</seniority>
    <createdAt>2026-10-01T08:00:00+00:00</createdAt>
    <jobDescriptions>
      <jobDescription><name>Deine Aufgaben</name><value><![CDATA[<ul><li>LLM &amp; RAG</li></ul>]]></value></jobDescription>
      <jobDescription><name>Dein Profil</name><value><![CDATA[<p>Python</p>]]></value></jobDescription>
    </jobDescriptions>
  </position>
  <position><id></id><name>Broken, no id</name></position>
</workzag-jobs>
"""


def test_personio_parse_feed():
    from honestapply.ats.personio import parse_feed

    jobs = parse_feed(PERSONIO_XML, "Acme", "acme.jobs.personio.de")
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "AI Engineer (m/w/d)"
    assert j["location"] == "München"
    assert j["url"] == "https://acme.jobs.personio.de/job/123"
    assert j["external_id"] == "123"
    assert "Deine Aufgaben: LLM & RAG" in j["description"]
    assert "Dein Profil: Python" in j["description"]
    assert j["source_board"] == "personio"


def test_personio_fetch_merges_translation_over_default(monkeypatch):
    from honestapply.ats import personio

    english = PERSONIO_XML.replace(b"AI Engineer (m/w/d)", b"AI Engineer (f/m/d)").replace(
        b"<value><![CDATA[<p>Python</p>]]></value>", b"<value></value>"
    )
    calls: list[dict | None] = []

    class _Resp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            return None

    def fake_get(url, params=None, **kw):
        calls.append(params)
        return _Resp(english if params else PERSONIO_XML)

    monkeypatch.setattr(personio.requests, "get", fake_get)
    jobs = personio.fetch_jobs(Employer(name="Acme", ats="personio", token="acme", language="en"))
    assert calls == [None, {"language": "en"}]
    assert jobs[0]["title"] == "AI Engineer (f/m/d)"  # translated title wins
    assert "Deine Aufgaben: LLM & RAG" in jobs[0]["description"]


def test_personio_custom_host():
    from honestapply.ats.personio import feed_host

    assert feed_host(Employer(name="Acme", ats="personio", token="acme")) == "acme.jobs.personio.de"
    assert feed_host(Employer(name="Acme", ats="personio", host="jobs.acme.com")) == "jobs.acme.com"


def test_recruitee_parse_offers():
    from honestapply.ats.recruitee import parse_offers

    payload = {
        "offers": [
            {
                "id": 7,
                "title": "Senior AI Engineer",
                "location": "Berlin, Berlin, Deutschland",
                "city": "Berlin",
                "country": "Deutschland",
                "remote": True,
                "careers_url": "https://acme.recruitee.com/o/senior-ai-engineer",
                "description": "<p>Build agents</p>",
                "requirements": "<p>Python</p>",
                "published_at": "2026-10-02 10:00:00 UTC",
                "department": "Tech",
                "company_name": "Acme GmbH",
            },
            {"id": 8, "title": "", "careers_url": "https://x"},
        ]
    }
    jobs = parse_offers(payload, "Acme")
    assert len(jobs) == 1
    j = jobs[0]
    assert j["company"] == "Acme GmbH"
    assert j["location"] == "Remote, Berlin, Berlin, Deutschland"
    assert j["description"] == "Build agents Python"
    assert j["url"].endswith("/o/senior-ai-engineer")
    assert j["external_id"] == "7"
    assert j["source_board"] == "recruitee"


def test_workable_parse_widget():
    from honestapply.ats.workable import parse_widget

    payload = {
        "name": "Acme",
        "jobs": [
            {
                "title": "AI Engineer",
                "shortcode": "AB12CD",
                "url": "https://apply.workable.com/acme/j/AB12CD/",
                "city": "Munich",
                "state": "Bavaria",
                "country": "Germany",
                "telecommuting": False,
                "description": "<p>LLM apps</p>",
                "requirements": "<p>Python</p>",
                "published_on": "2026-10-03",
            },
            {"title": "Remote ML Engineer", "shortcode": "ZZ", "url": "https://apply.workable.com/acme/j/ZZ/", "telecommuting": True},
        ],
    }
    jobs = parse_widget(payload, "Acme")
    assert [j["location"] for j in jobs] == ["Munich, Bavaria, Germany", "Remote"]
    assert jobs[0]["description"] == "LLM apps Python"
    assert jobs[0]["external_id"] == "AB12CD"
    assert jobs[0]["source_board"] == "workable"


def test_teamtailor_parse_feed_locations():
    from honestapply.ats.teamtailor import parse_feed

    payload = {
        "items": [
            {
                "id": "guid-1",
                "title": "Machine Learning Engineer",
                "url": "https://acme.teamtailor.com/jobs/1-ml",
                "date_published": "2026-10-04T09:00:00+02:00",
                "content_html": "<p>Train &amp; ship</p>",
                "_jobposting": {
                    "jobLocation": [
                        {"address": {"addressLocality": "Hamburg", "addressCountry": "DE"}},
                        {"address": {"addressLocality": "Berlin", "addressCountry": {"name": "Germany"}}},
                    ],
                },
            },
            {
                "id": "guid-2",
                "title": "Remote AI Engineer",
                "url": "https://acme.teamtailor.com/jobs/2-ai",
                "_jobposting": {"jobLocationType": "TELECOMMUTE", "jobLocation": {"address": {"addressLocality": "Stockholm"}}},
            },
        ]
    }
    jobs = parse_feed(payload, "Acme")
    assert jobs[0]["location"] == "Hamburg, DE; Berlin, Germany"
    assert jobs[0]["description"] == "Train & ship"
    assert jobs[1]["location"] == "Remote, Stockholm"
    assert jobs[1]["source_board"] == "teamtailor"


def test_detect_new_hosts_and_source_only():
    from honestapply.ats.detect import detect_ats, is_source_only

    assert detect_ats("https://acme.jobs.personio.de/job/1") == "personio"
    assert detect_ats("https://acme.recruitee.com/o/x") == "recruitee"
    assert detect_ats("https://apply.workable.com/acme/j/AB/") == "workable"
    assert detect_ats("https://acme.teamtailor.com/jobs/1-x") == "teamtailor"
    assert detect_ats("https://join.com/companies/acme/1-x") == "join"
    aa = detect_ats("https://www.arbeitsagentur.de/jobsuche/jobdetail/123-S")
    assert aa == "arbeitsagentur" and is_source_only(aa)
    assert not is_source_only("greenhouse")
    assert not is_source_only(None)


def test_employer_schema_accepts_new_ats():
    for ats in ("personio", "recruitee", "workable", "teamtailor"):
        assert Employer(name="Acme", ats=ats, token="acme").ats == ats
