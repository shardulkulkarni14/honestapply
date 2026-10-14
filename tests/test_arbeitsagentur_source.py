"""Arbeitsagentur (Bundesagentur für Arbeit Jobbörse) discovery source.

Offline: the HTTP layer is monkeypatched, so no network is touched. The sample
payloads mirror the real ``pc/v6/jobs`` response shape.
"""

from __future__ import annotations

from types import SimpleNamespace

from honestapply.stages import arbeitsagentur as aa

# One real-shaped result item (trimmed to the fields the parser reads).
SAMPLE_ITEM = {
    "stellenangebotsTitel": "AI Engineer (m/d/w)",
    "firma": "EXXETA AG",
    "referenznummer": "12336-a26f1192j0456062-S",
    "externeURL": "https://gute-jobs.de/viewjob-nsx8n?utm_source=x",
    "datumErsteVeroeffentlichung": "2026-10-14",
    "verguetungsangabe": "KEINE_ANGABEN",
    "homeofficemoeglich": False,
    "stellenlokationen": [
        {"adresse": {"plz": "81929", "ort": "München", "region": "BAYERN", "land": "DEUTSCHLAND"}}
    ],
}


def test_parse_item_maps_core_fields_and_prefers_external_url():
    job = aa.parse_item(SAMPLE_ITEM)
    assert job is not None
    assert job["title"] == "AI Engineer (m/d/w)"
    assert job["company"] == "EXXETA AG"
    assert job["source_board"] == "arbeitsagentur"
    assert job["external_id"] == "12336-a26f1192j0456062-S"
    assert job["url"] == "https://gute-jobs.de/viewjob-nsx8n?utm_source=x"
    assert job["location"] == "München, Germany"
    assert job["posted_at"] == "2026-10-14"
    # KEINE_ANGABEN is a "no data" sentinel → no salary text.
    assert job["salary_range_text"] is None
    # Description is fetched later by the enrich stage.
    assert job["description"] is None


def test_parse_item_builds_detail_url_when_no_external():
    item = dict(SAMPLE_ITEM)
    item.pop("externeURL")
    job = aa.parse_item(item)
    assert job["url"] == (
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
        "12336-a26f1192j0456062-S"
    )


def test_parse_item_location_fallbacks():
    remote = {**SAMPLE_ITEM, "stellenlokationen": [], "homeofficemoeglich": True}
    assert aa.parse_item(remote)["location"] == "Remote (Germany)"
    bare = {**SAMPLE_ITEM, "stellenlokationen": [], "homeofficemoeglich": False}
    assert aa.parse_item(bare)["location"] == "Germany"


def test_parse_item_rejects_incomplete():
    assert aa.parse_item({"firma": "ACME"}) is None          # no title
    assert aa.parse_item({"stellenangebotsTitel": "X"}) is None  # no company/url
    assert aa.parse_item("not a dict") is None


def test_parse_item_keeps_real_salary():
    item = {**SAMPLE_ITEM, "verguetungsangabe": "60.000 - 80.000 EUR/Jahr"}
    assert aa.parse_item(item)["salary_range_text"] == "60.000 - 80.000 EUR/Jahr"


class _FakeResp:
    def __init__(self, items, status=200):
        self._items = items
        self.status_code = status

    def json(self):
        return {"ergebnisliste": self._items, "maxErgebnisse": len(self._items)}


def _mk(ref):
    return {**SAMPLE_ITEM, "referenznummer": ref}


def test_run_search_dedups_within_a_page(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResp([_mk("A"), _mk("B"), _mk("A"), _mk("C")])  # "A" repeated

    monkeypatch.setattr(aa.requests, "get", fake_get)
    entry = SimpleNamespace(query="AI Engineer", location="München", max_results=100, hours_old=168)
    assert [j["external_id"] for j in aa.run_arbeitsagentur_search(entry)] == ["A", "B", "C"]


def test_run_search_respects_max_results(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResp([_mk("A"), _mk("B"), _mk("C")])

    monkeypatch.setattr(aa.requests, "get", fake_get)
    entry = SimpleNamespace(query="x", location="", max_results=2, hours_old=None)
    assert [j["external_id"] for j in aa.run_arbeitsagentur_search(entry)] == ["A", "B"]


def test_run_search_paginates_and_dedups_across_pages(monkeypatch):
    # A full first page (== page size) forces a second request; page 2 repeats
    # one refnr from page 1 and adds one new, which must dedup to +1.
    full_page = [_mk(f"p1_{i}") for i in range(aa._PAGE_SIZE)]

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResp(full_page if params.get("page") == "1" else [_mk("p1_0"), _mk("NEW")])

    monkeypatch.setattr(aa.requests, "get", fake_get)
    entry = SimpleNamespace(query="x", location="", max_results=250, hours_old=None)
    jobs = aa.run_arbeitsagentur_search(entry)
    refs = [j["external_id"] for j in jobs]
    assert len(refs) == aa._PAGE_SIZE + 1   # the duplicate p1_0 was dropped
    assert refs[-1] == "NEW"


def test_run_search_stops_on_http_error(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResp([], status=403)

    monkeypatch.setattr(aa.requests, "get", fake_get)
    entry = SimpleNamespace(query="x", location="", max_results=50, hours_old=None)
    assert aa.run_arbeitsagentur_search(entry) == []
