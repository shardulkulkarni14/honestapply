"""Find an employer's own posting for a job discovered on a board that cannot be
applied through.

The Arbeitsagentur Jobbörse is honestapply's best German source, but its listing
page hides the employer's contact and apply link behind an image CAPTCHA. A job
whose only URL is that page has no automatable form. Many of those employers
run a public ATS board, though, so before giving up we look for the same title
on the boards we can read without auth (Greenhouse, Lever, Ashby,
SmartRecruiters, Personio, Recruitee, Workable, Teamtailor) under a few slug
guesses derived from the company name.

Everything here is plain HTTP and string matching — no LLM, no scraping of
pages that forbid it. Results are cached per company in ``data/`` so a board is
probed once, not on every discovery run.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from honestapply.logging_setup import get_logger

log = get_logger(__name__)

_TIMEOUT = 12
_HEADERS = {"User-Agent": "honestapply/1.0 (+https://github.com/honestapply)"}
# A negative answer ("no board found") is re-checked after this many days.
_NEGATIVE_TTL_DAYS = 30
_MIN_SLUG_LEN = 3

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "é": "e", "è": "e", "á": "a"})
# Words that are legal-form or filler, dropped to form the "core" slug.
_LEGAL_WORDS = {
    "gmbh", "ag", "se", "kg", "kgaa", "co", "ohg", "mbh", "ug", "inc", "ltd", "llc", "plc",
    "holding", "group", "gruppe", "deutschland", "germany", "europe", "international",
    "consulting", "services", "solutions", "technologies", "technology", "software",
    "and", "und", "the", "die", "der", "das",
}
_GENDER_MARKER = re.compile(r"\((?:[mwfdxh]\s*[/|]\s*)+[mwfdxh*]?\)|\(all\s+genders?\)|\(gn\)|\(m/w/d\)", re.I)
_TITLE_STOP = {"m", "w", "d", "f", "x", "gn", "all", "genders", "gender", "and", "und", "for", "fuer",
               "fur", "the", "in", "mit", "with", "of", "de", "en", "a", "an", "als", "zum", "zur"}


def _ascii(text: str) -> str:
    return (text or "").lower().translate(_UMLAUTS)


def slug_candidates(company: str, hint_domain: str | None = None) -> list[str]:
    """Ordered, de-duplicated board-slug guesses for a company name.

    "Capgemini Deutschland GmbH" -> ["capgeminideutschlandgmbh", "capgemini"];
    "REWE Group" with hint "www.rewe-group.com" -> ["rewegroup", "rewe", "rewe-group"].
    """
    words = re.findall(r"[a-z0-9]+", _ascii(company).replace("&", " "))
    core = [w for w in words if w not in _LEGAL_WORDS] or words
    cands: list[str] = []
    if words:
        cands.append("".join(words))
    if core:
        cands.append("".join(core))
        cands.append("-".join(core))
        if len(core) > 1:
            cands.append(core[0])
    if hint_domain:
        label = _ascii(hint_domain).strip().strip("/")
        label = re.sub(r"^https?://", "", label)
        label = label.split("/")[0]
        parts = [p for p in label.split(".") if p and p not in {"www", "jobs", "karriere", "careers", "career"}]
        if len(parts) >= 2:
            cands.append(parts[-2])
            cands.append(parts[-2].replace("-", ""))
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        if len(c) >= _MIN_SLUG_LEN and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def title_tokens(title: str) -> set[str]:
    text = _GENDER_MARKER.sub(" ", _ascii(title))
    return {t for t in re.findall(r"[a-z0-9]+", text) if t not in _TITLE_STOP}


def title_similarity(a: str, b: str) -> float:
    """Jaccard overlap of the two titles' tokens; 0.9 when one contains the other."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    if ta <= tb or tb <= ta:
        return 0.9
    return len(ta & tb) / len(ta | tb)


# --- board probes ---------------------------------------------------------
# Each returns a list of (title, url) for the slug, or None when the board
# does not exist (404 / bad shape). SmartRecruiters answers 200 with an empty
# page for *any* identifier, so an empty board is treated as "no board" there
# and, for consistency, everywhere: an employer with zero open roles is of no
# use to the resolver anyway, and must not stop the search on other ATSs.

def _get_json(url: str, params: dict | None = None) -> Any | None:
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _probe_greenhouse(slug: str) -> list[tuple[str, str]] | None:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return [(j.get("title") or "", j.get("absolute_url") or "") for j in data["jobs"]]


def _probe_lever(slug: str) -> list[tuple[str, str]] | None:
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}", {"mode": "json"})
    if not isinstance(data, list):
        return None
    return [(j.get("text") or "", j.get("hostedUrl") or "") for j in data if isinstance(j, dict)]


def _probe_ashby(slug: str) -> list[tuple[str, str]] | None:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return [(j.get("title") or "", j.get("jobUrl") or "") for j in data["jobs"]]


_SR_PAGE = 100
_SR_MAX_PAGES = 5


def _probe_smartrecruiters(slug: str) -> list[tuple[str, str]] | None:
    """Paginated: big tenants (ALTEN, Bosch) list well over 100 postings."""
    out: list[tuple[str, str]] = []
    for page in range(_SR_MAX_PAGES):
        data = _get_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            {"limit": str(_SR_PAGE), "offset": str(page * _SR_PAGE)},
        )
        if not isinstance(data, dict) or "content" not in data:
            return out or None
        for j in data["content"]:
            company = (j.get("company") or {}).get("identifier") or slug
            out.append((j.get("name") or "", f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}"))
        if len(data["content"]) < _SR_PAGE:
            break
    return out


def _probe_personio(slug: str) -> list[tuple[str, str]] | None:
    from honestapply.ats.personio import parse_feed

    host = f"{slug}.jobs.personio.de"
    try:
        resp = requests.get(f"https://{host}/xml", headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or b"<workzag-jobs" not in resp.content[:2000]:
        return None
    try:
        return [(j["title"], j["url"]) for j in parse_feed(resp.content, slug, host)]
    except Exception:
        return None


def _probe_recruitee(slug: str) -> list[tuple[str, str]] | None:
    from honestapply.ats.recruitee import parse_offers

    data = _get_json(f"https://{slug}.recruitee.com/api/offers/")
    if not isinstance(data, dict) or "offers" not in data:
        return None
    return [(j["title"], j["url"]) for j in parse_offers(data, slug)]


def _probe_workable(slug: str) -> list[tuple[str, str]] | None:
    from honestapply.ats.workable import parse_widget

    data = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return [(j["title"], j["url"]) for j in parse_widget(data, slug)]


def _probe_teamtailor(slug: str) -> list[tuple[str, str]] | None:
    from honestapply.ats.teamtailor import parse_feed

    data = _get_json(f"https://{slug}.teamtailor.com/jobs.json")
    if not isinstance(data, dict) or "items" not in data:
        return None
    return [(j["title"], j["url"]) for j in parse_feed(data, slug)]


PROBES: dict[str, Callable[[str], list[tuple[str, str]] | None]] = {
    "greenhouse": _probe_greenhouse,
    "lever": _probe_lever,
    "ashby": _probe_ashby,
    "personio": _probe_personio,
    "recruitee": _probe_recruitee,
    "workable": _probe_workable,
    "teamtailor": _probe_teamtailor,
    "smartrecruiters": _probe_smartrecruiters,  # last: it never 404s
}


# --- resolver -------------------------------------------------------------

def _company_key(company: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", _ascii(company)))


@dataclass
class BoardResolver:
    """Resolve (company, title) to the employer's own posting URL.

    ``cache_path`` persists which board (if any) a company uses, so the probes
    run once per company rather than once per listing per run. Board contents
    are memoised for the lifetime of the resolver (one discovery run).
    """

    cache_path: Path | None = None
    min_similarity: float = 0.6
    probes: dict[str, Callable[[str], list[tuple[str, str]] | None]] = field(default_factory=lambda: dict(PROBES))
    _cache: dict[str, dict] = field(default_factory=dict, init=False)
    _boards: dict[tuple[str, str], list[tuple[str, str]]] = field(default_factory=dict, init=False)
    _loaded: bool = field(default=False, init=False)

    # -- cache -------------------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._cache = {}

    def _save(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=1, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            log.warning("resolve.cache_write_failed", error=str(exc))

    # -- lookup ------------------------------------------------------------
    def _board_jobs(self, ats: str, slug: str) -> list[tuple[str, str]] | None:
        key = (ats, slug)
        if key not in self._boards:
            probe = self.probes.get(ats)
            jobs = probe(slug) if probe else None
            self._boards[key] = jobs or None  # empty board == no board
        return self._boards[key]

    def candidate_boards(self, company: str, hint_domain: str | None = None) -> list[tuple[str, str]]:
        """Every (ats, slug) that answers with open jobs for this company's slug guesses.

        A short guess such as the company's first word ("amazon", "genius") can
        land on an unrelated tenant, so no board is trusted on its own: the
        caller confirms one by matching a title. Probing happens once per
        company; the list (or its absence) is cached.
        """
        self._load()
        key = _company_key(company)
        entry = self._cache.get(key)
        if entry:
            if entry.get("ats"):
                return [(entry["ats"], entry["token"])]
            if time.time() - float(entry.get("checked", 0)) < _NEGATIVE_TTL_DAYS * 86400:
                return [tuple(b) for b in entry.get("boards") or []]
        boards: list[tuple[str, str]] = []
        for slug in slug_candidates(company, hint_domain):
            for ats in self.probes:
                if self._board_jobs(ats, slug):
                    boards.append((ats, slug))
        self._cache[key] = {"ats": None, "company": company, "boards": boards, "checked": time.time()}
        self._save()
        if boards:
            log.info("resolve.boards_found", company=company, boards=boards)
        return boards

    def find_board(self, company: str, hint_domain: str | None = None) -> tuple[str, str] | None:
        """The confirmed board for this company, or the first candidate when none
        has been confirmed by a title match yet. None when no board answers."""
        boards = self.candidate_boards(company, hint_domain)
        return boards[0] if boards else None

    def resolve(self, company: str, title: str, hint_domain: str | None = None) -> dict | None:
        """The employer's own posting for this title, or None.

        Returns ``{"url", "ats", "token", "title", "similarity"}``. The board
        that yields a match is remembered as the company's board.
        """
        best: tuple[float, str, str, str, str] | None = None
        for ats, slug in self.candidate_boards(company, hint_domain):
            for other_title, url in self._board_jobs(ats, slug) or []:
                if not url:
                    continue
                sim = title_similarity(title, other_title)
                if best is None or sim > best[0]:
                    best = (sim, other_title, url, ats, slug)
        if not best or best[0] < self.min_similarity:
            return None
        sim, other_title, url, ats, slug = best
        key = _company_key(company)
        if not (self._cache.get(key) or {}).get("ats"):
            self._cache[key] = {"ats": ats, "token": slug, "company": company, "checked": time.time()}
            self._save()
            log.info("resolve.board_confirmed", company=company, ats=ats, token=slug)
        return {"url": url, "ats": ats, "token": slug, "title": other_title, "similarity": round(sim, 2)}
