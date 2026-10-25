"""Employer-board resolver: slug guesses, title matching, cached probing, and
the Arbeitsagentur relink hook. No network: probes are injected."""

from __future__ import annotations

import json

from honestapply.ats.resolve import BoardResolver, slug_candidates, title_similarity


def test_slug_candidates_strip_legal_forms_and_use_domain_hint():
    assert slug_candidates("Capgemini Deutschland GmbH") == ["capgeminideutschlandgmbh", "capgemini"]
    assert slug_candidates("REWE Group", "www.rewe-group.com") == ["rewegroup", "rewe", "rewe-group"]
    cands = slug_candidates("MONTANA Energie-Handel GmbH & Co. KG")
    assert cands[0] == "montanaenergiehandelgmbhcokg" and "montana" in cands
    assert slug_candidates("Müller AG")[0] == "muellerag"
    assert slug_candidates("AG") == []  # too short / nothing left


def test_title_similarity_ignores_gender_markers_and_seniority_noise():
    assert title_similarity("(Senior) AI Engineer GenAI (m/w/d)", "Senior AI Engineer GenAI (m/w/d) Köln") == 0.9
    assert title_similarity("AI Engineer (m/w/d)", "AI Engineer (f/m/x)") == 1.0
    assert title_similarity("Technical Gen AI Solutions Consultant (w/m/d)", "Agentic AI Consultant Public Sector") < 0.6
    assert title_similarity("", "AI Engineer") == 0.0


def _resolver(tmp_path, boards):
    """boards: {(ats, slug): [(title, url), ...]}; anything else is 'no board'."""
    calls: list[tuple[str, str]] = []

    def make(ats):
        def probe(slug):
            calls.append((ats, slug))
            return boards.get((ats, slug))

        return probe

    r = BoardResolver(cache_path=tmp_path / "boards.json", probes={a: make(a) for a in ("greenhouse", "personio")})
    return r, calls


def test_resolve_finds_posting_and_caches_board(tmp_path):
    boards = {("personio", "acme"): [("Senior AI Engineer (m/w/d)", "https://acme.jobs.personio.de/job/1"),
                                     ("Office Manager", "https://acme.jobs.personio.de/job/2")]}
    r, calls = _resolver(tmp_path, boards)
    hit = r.resolve("Acme GmbH", "AI Engineer (m/w/d)")
    assert hit["url"] == "https://acme.jobs.personio.de/job/1"
    assert hit["ats"] == "personio" and hit["similarity"] == 0.9
    # unrelated title on a known board -> no hit, and no re-probing
    n = len(calls)
    assert r.resolve("Acme GmbH", "Head of Marketing") is None
    assert len(calls) == n
    cached = json.loads((tmp_path / "boards.json").read_text())
    assert cached["acmegmbh"]["ats"] == "personio" and cached["acmegmbh"]["token"] == "acme"


def test_negative_result_is_cached_across_resolvers(tmp_path):
    r, calls = _resolver(tmp_path, {})
    assert r.resolve("Nowhere Corp", "AI Engineer") is None
    assert calls  # probed at least once
    r2, calls2 = _resolver(tmp_path, {})
    assert r2.resolve("Nowhere Corp", "AI Engineer") is None
    assert calls2 == []  # negative cache honoured


def test_attach_employer_links_relinks_only_listing_pages(tmp_path):
    from honestapply.stages.arbeitsagentur import attach_employer_links

    boards = {("greenhouse", "acme"): [("AI Engineer", "https://boards.greenhouse.io/acme/jobs/9")]}
    r, _ = _resolver(tmp_path, boards)
    results = [
        {"external_id": "1-S", "company": "Acme", "title": "AI Engineer (m/w/d)",
         "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/1-S"},
        {"external_id": "2-S", "company": "Acme", "title": "AI Engineer (m/w/d)",
         "url": "https://www.heyjobs.co/de-de/jobs/abc"},  # already has an employer/aggregator link
        {"external_id": "3-S", "company": "Acme", "title": "AI Engineer (m/w/d)",
         "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/3-S"},  # known -> skipped
    ]
    relinked = attach_employer_links(results, r, skip_ids={"3-S"})
    assert relinked == 1
    assert results[0]["url"] == "https://boards.greenhouse.io/acme/jobs/9"
    assert results[0]["_resolved_board"] == "greenhouse"
    assert results[1]["url"].startswith("https://www.heyjobs.co/")
    assert results[2]["url"].endswith("/3-S")


def test_attach_employer_links_survives_resolver_errors():
    from honestapply.stages.arbeitsagentur import attach_employer_links

    class Boom:
        def resolve(self, *a, **k):
            raise RuntimeError("network down")

    results = [{"external_id": "1-S", "company": "Acme", "title": "AI Engineer",
                "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/1-S"}]
    assert attach_employer_links(results, Boom()) == 0
    assert results[0]["url"].endswith("/1-S")


def test_empty_board_does_not_stop_the_search(tmp_path):
    # SmartRecruiters answers 200 + no postings for any identifier; that must
    # not be cached as "the" board when another ATS actually lists the jobs.
    boards = {("greenhouse", "acmegmbh"): [], ("personio", "acme"): [("AI Engineer", "https://acme.jobs.personio.de/job/1")]}
    r, _ = _resolver(tmp_path, boards)
    hit = r.resolve("Acme GmbH", "AI Engineer")
    assert hit and hit["ats"] == "personio"
