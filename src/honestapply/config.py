"""Central configuration for honestapply.

Precedence: environment / .env  >  YAML/JSON config files  >  code defaults.

The `Settings` object (pydantic-settings) holds runtime knobs read from the
environment. The `load_*` helpers parse the user's YAML/JSON config files into
typed Pydantic models. All file models use `extra="allow"` so users can extend
the example files without breaking parsing.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Hard safety ceiling. Not user-overridable. ---------------------------
# An absolute upper bound on real submissions per day, independent of
# `honestapply_daily_cap`. Deliberately conservative: evidence is that tailored,
# low-volume applying converts far better than mass applying, and high per-day
# volume from one origin is exactly what ATS-side spam detection looks for.
# Raising this is a decision about someone else's inbox as well as your own.
HARD_DAILY_CEILING = 50

ProviderName = Literal["claude_cli", "anthropic", "gemini", "openai", "stub"]


# ---------------------------------------------------------------------------
# Environment-driven settings
# ---------------------------------------------------------------------------
class Settings(BaseSettings):
    """Runtime settings sourced from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM
    llm_provider: ProviderName = "anthropic"
    claude_cli_model: str = "sonnet"  # CLI alias for the key-free claude_cli provider
    anthropic_model: str = "claude-sonnet-4-6"
    gemini_model: str = "gemini-2.0-flash"
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    openai_api_key: str | None = None
    # Point at any OpenAI-compatible server to use it instead of OpenAI itself:
    # Ollama (http://localhost:11434/v1), LM Studio, vLLM, llama.cpp, OpenRouter,
    # Groq, Together, DeepSeek. Local servers need no key.
    openai_base_url: str | None = None

    # Paths
    honestapply_db_path: str = "data/honestapply.db"
    honestapply_browser_profile: str = "~/.honestapply/browser-profile"

    # Safety knobs
    honestapply_rate_limit_seconds: int = 90
    honestapply_rate_limit_jitter_seconds: int = 30
    honestapply_daily_cap: int = 25
    honestapply_min_score: int = 7
    honestapply_dry_run_first_n: int = 3
    # Applying to one employer many times in a row reads as spraying, not
    # interest. An id-ordered queue groups a company's roles together, which can
    # send several back-to-back applications to the same employer in one run. Two
    # guards prevent that: company interleaving in the apply queue, and this
    # rolling-24h per-employer cap.
    honestapply_per_company_daily_cap: int = 2
    # The 24h cap resets overnight, so a company can still accumulate applications
    # day after day. This cumulative lifetime cap retires a company's remaining
    # queued roles once it has this many real submissions in total, so the
    # pipeline never keeps re-applying to one employer. Set to 0 to disable.
    honestapply_per_company_total_cap: int = 2

    # Application conventions (CV format, salary/date formatting, salutations,
    # work-auth phrasing, account-walled ATSes). Defaults to the German market —
    # the beachhead — and is overridable; "en" gives neutral international.
    honestapply_market: str = "de-DE"

    # --- Prepare-pipeline concurrency (enrich / score / tailor / cover) -------
    # How many jobs the prepare stages work on at once. The stages are bound by
    # the LLM round-trip (`claude -p` spawns a process per call and takes
    # 10-60s), so a small thread pool overlaps those waits. Each job still runs
    # its stages in order and commits per stage; the DB is the queue and a
    # per-stage compare-and-set keeps two workers from doing the same work.
    # Ships at 1 — identical to the serial behaviour — so nothing changes until
    # you raise it. 3 is the recommended value: enough to hide the LLM latency
    # without running into Claude Code's own usage limits. The apply stage is
    # never parallel (rate limits and the browser profile are per-process).
    honestapply_prepare_workers: int = 1
    # Score-only provider override (e.g. "anthropic" to score over the API while
    # tailor/cover keep using claude_cli). None = use `llm_provider` everywhere.
    honestapply_score_provider: str | None = None
    # When a worker hits an LLM usage/rate limit, every worker pauses this long
    # before its next LLM call instead of burning the remaining candidates.
    honestapply_llm_pause_seconds: int = 120
    # Give up the whole run after this many consecutive pauses with no successful
    # LLM call in between — a persistent usage-window limit should abort fast, not
    # sleep `pause_seconds` before every one of hundreds of queued jobs. 0 = never
    # give up (sleep indefinitely).
    honestapply_llm_pause_give_up_after: int = 3

    # --- Derived helpers ---
    @property
    def effective_daily_cap(self) -> int:
        return min(self.honestapply_daily_cap, HARD_DAILY_CEILING)

    @property
    def market(self):
        """The configured Market (conventions for the target country)."""
        from honestapply.markets.registry import get_market

        return get_market(self.honestapply_market)

    @property
    def db_path(self) -> Path:
        return Path(os.path.expanduser(self.honestapply_db_path)).resolve()

    @property
    def browser_profile_dir(self) -> Path:
        return Path(os.path.expanduser(self.honestapply_browser_profile)).resolve()

    def api_key_for(self, provider: str | None = None) -> str | None:
        provider = provider or self.llm_provider
        return {
            "claude_cli": "claude_cli",  # no key needed — uses local Claude Code auth
            "anthropic": self.anthropic_api_key,
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
            "stub": "stub",
        }.get(provider)

    def model_for(self, provider: str | None = None) -> str:
        provider = provider or self.llm_provider
        return {
            "claude_cli": self.claude_cli_model,
            "anthropic": self.anthropic_model,
            "gemini": self.gemini_model,
            "openai": self.openai_model,
            "stub": "stub",
        }.get(provider, self.anthropic_model)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
def project_root() -> Path:
    """Repo root = parent of the `data/` dir we ship. Resolve from CWD upward."""
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "honestapply").exists():
            return candidate
    return cwd


class Paths:
    """Convenience accessors for well-known project directories."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or project_root()

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def resumes_dir(self) -> Path:
        return self.data_dir / "resumes"

    @property
    def recommendations_dir(self) -> Path:
        return self.data_dir / "recommendations"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    def job_output_dir(self, job_id: int | str) -> Path:
        d = self.outputs_dir / str(job_id)
        d.mkdir(parents=True, exist_ok=True)
        return d


PATHS = Paths()


# ---------------------------------------------------------------------------
# Config-file models (parsed from YAML/JSON the user edits)
# ---------------------------------------------------------------------------
class SearchEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    query: str
    location: str = ""
    board: str = "indeed"  # indeed | linkedin | glassdoor | zip_recruiter | google
    max_results: int = 25
    country: str = "Germany"
    hours_old: int | None = None
    is_remote: bool = False


class Employer(BaseModel):
    """A company whose ATS board we scrape directly via its public read API.

    "instahyre" is not a single-company ATS but an India job-board aggregator:
    one entry yields postings across many companies, each carrying its own
    company name (see honestapply.ats.instahyre). It shares this dispatch path
    because discover routes on `ats` -> `_ATS_MODULES`.
    """

    model_config = ConfigDict(extra="allow")
    name: str
    ats: Literal[
        "greenhouse", "lever", "ashby", "smartrecruiters", "workday", "instahyre"
    ]
    # The board identifier in that ATS's URL (e.g. greenhouse board token,
    # lever site, ashby org slug, SmartRecruiters company identifier).
    # Defaults to a lowercased name guess.
    token: str = ""
    # Optional, used by some ATS fetchers (extra="allow" also accepts them):
    #   smartrecruiters: `country` (ISO code, default "de"), `q` (keyword filter)
    #   workday:        `host` (e.g. "acme.wd3.myworkdayjobs.com"), `site`
    #                   (career-site path, e.g. "External"), `tenant` (defaults to
    #                   the host's first sub-domain label)

    def board_token(self) -> str:
        return self.token or self.name.lower().replace(" ", "")


class Profile(BaseModel):
    """Applicant profile. Permissive — mirrors config/profile.example.json."""

    model_config = ConfigDict(extra="allow")
    legal_name: dict[str, Any] = Field(default_factory=dict)
    email: str = ""
    phone: str = ""
    address: dict[str, Any] = Field(default_factory=dict)
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    work_authorization: dict[str, Any] = Field(default_factory=dict)
    demographics: dict[str, Any] = Field(default_factory=dict)
    salary_expectation: dict[str, Any] = Field(default_factory=dict)
    notice_period_weeks: int | None = None
    available_start_date: str = ""
    preferred_locations: list[str] = Field(default_factory=list)
    willing_to_relocate: bool = False
    references: list[dict[str, Any]] = Field(default_factory=list)
    screening_question_defaults: dict[str, Any] = Field(default_factory=dict)
    # Optional supporting documents (transcripts, reference letters, work
    # references / Arbeitszeugnis). Paths are absolute or relative to repo root;
    # `honestapply docs-bundle` merges them into a single PDF for forms that require
    # one combined "Transcripts & Reference Letters" upload.
    supporting_documents: list[str] = Field(default_factory=list)
    # Canonical CEFR language levels, e.g. {"english": "C2", "german": "A2"}.
    # The single source of truth for the no-over-claim guard: cover letters and
    # résumés may never assert a higher level than these. Caps whatever the
    # résumé YAML lists, so an over-stated YAML can't leak a false fluency claim.
    language_levels: dict[str, str] = Field(default_factory=dict)

    def summary_for_scoring(self, resume: Any | None = None) -> str:
        """The candidate block of the score prompt.

        The score prompt asks the model to match the candidate's *skills and
        experience* against the posting, so those have to be in here — for a
        long time this returned only name, locations, work authorization and
        salary, and every fit score was a guess about an unknown person. The
        background comes from the résumé YAML (``data/resumes/default.yaml`` by
        default, or *resume* if given): headline, summary, skills groups, a
        condensed experience list, education, languages and target keywords.
        Nothing is synthesised — every line is lifted from the user's own files,
        and the profile's own ``professional_summary``/``skills`` are the
        fallback when no résumé exists yet.
        """
        name = " ".join(
            str(self.legal_name.get(k, "")) for k in ("first", "last")
        ).strip()
        locs = ", ".join(self.preferred_locations)
        head = (
            f"Name: {name}\n"
            f"Preferred locations: {locs}\n"
            f"Work authorization: {self.work_authorization}\n"
            f"Salary expectation: {self.salary_expectation}\n"
        )
        if resume is None:
            resume = load_default_resume()
        background = (
            _resume_background(resume, self.language_levels)
            if resume is not None
            else self._profile_background()
        )
        return head + ("\n" + background + "\n" if background else "")

    def _profile_background(self) -> str:
        """Skills/summary straight from profile.json — used when no résumé YAML
        exists yet. Placeholder values from the example file are dropped."""
        lines: list[str] = []
        summary = str(getattr(self, "professional_summary", "") or "")
        if summary and not _is_placeholder(summary):
            lines.append(f"Summary: {_squash(summary, 600)}")
        skills = getattr(self, "skills", None)
        if isinstance(skills, dict):
            groups = [
                f"  - {k}: {_join(v)}"
                for k, v in skills.items()
                if not str(k).startswith("_") and v and not _is_placeholder(_join(v))
            ]
            if groups:
                lines.append("Skills:")
                lines.extend(groups)
        return "\n".join(lines)


# --- Candidate background for scoring ----------------------------------------
_SCORING_BULLET_CHARS = 180
_SCORING_SUMMARY_CHARS = 600


def _squash(text: str, limit: int) -> str:
    """Collapse whitespace and cap length (with an ellipsis) for prompt lines."""
    out = " ".join(str(text or "").split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _join(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if str(v).strip())
    return str(value or "")


def _is_placeholder(text: str) -> bool:
    return text.strip().upper().startswith("TODO")


def default_resume_path() -> Path | None:
    """``data/resumes/default.yaml`` if present, else the first résumé YAML there."""
    d = PATHS.resumes_dir
    preferred = d / "default.yaml"
    if preferred.exists():
        return preferred
    if not d.exists():
        return None
    return next(iter(sorted([*d.glob("*.yaml"), *d.glob("*.yml")])), None)


def load_default_resume() -> Any | None:
    """The default base résumé, or None when none exists / it fails to parse."""
    path = default_resume_path()
    if path is None:
        return None
    try:
        from honestapply.resume.schema import load_resume

        return load_resume(path)
    except Exception:  # noqa: BLE001 — scoring must still work without a résumé
        return None


def _resume_background(resume: Any, language_levels: dict[str, str] | None = None) -> str:
    """Compact, truthful candidate background lifted verbatim (trimmed) from a
    résumé YAML: headline, summary, skills, experience, education, languages,
    certifications and target keywords. Kept short — the posting is the long
    part of the prompt — but complete enough that a fit score means something.

    *language_levels* (the profile's authoritative CEFR map) overrides the
    résumé's free-text languages so the scorer never sees a bare "German" that
    could inflate fit on a German-required role — the same no-over-claim guard
    the cover-letter and tailor stages already apply."""
    facts = resume.resume_facts
    lines: list[str] = []

    headline = (facts.contact.title or "").strip()
    if headline and not _is_placeholder(headline):
        lines.append(f"Headline: {_squash(headline, 160)}")
    if resume.summary_variants and not _is_placeholder(resume.summary_variants[0]):
        lines.append(f"Summary: {_squash(resume.summary_variants[0], _SCORING_SUMMARY_CHARS)}")

    if facts.skills:
        lines.append("Skills:")
        for group, items in facts.skills.items():
            joined = _join(items)
            if joined.strip():
                lines.append(f"  - {group}: {_squash(joined, 240)}")

    exp_lines = []
    for exp in facts.experience or []:
        who = " · ".join(p for p in (exp.company, exp.title) if p)
        if not who or _is_placeholder(who):
            continue
        when = f" ({exp.dates})" if exp.dates else ""
        first = _squash(exp.bullets[0], _SCORING_BULLET_CHARS) if exp.bullets else ""
        exp_lines.append(f"  - {who}{when}" + (f": {first}" if first else ""))
    if exp_lines:
        lines.append("Experience (most recent first):")
        lines.extend(exp_lines)

    if facts.education:
        edu = "; ".join(
            ", ".join(p for p in (e.degree, e.institution) if p)
            + (f" ({e.dates})" if e.dates else "")
            for e in facts.education
        )
        if edu:
            lines.append(f"Education: {edu}")
    if language_levels:
        # Authoritative CEFR levels from the profile — capped source of truth.
        langs = ", ".join(
            f"{str(name).capitalize()} ({lvl})" for name, lvl in language_levels.items() if name
        )
        if langs:
            lines.append(f"Languages: {langs}")
    elif facts.languages:
        lines.append(f"Languages: {_join(facts.languages)}")
    if facts.certifications:
        lines.append(f"Certifications: {_join(facts.certifications)}")
    if resume.target_keywords:
        lines.append(f"Target keywords: {_join(resume.target_keywords)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _read_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    if path.suffix == ".json":
        return json.loads(text)
    raise ValueError(f"Unsupported config format: {path.suffix}")


def _first_existing(*names: str) -> Path | None:
    for name in names:
        p = PATHS.config_dir / name
        if p.exists():
            return p
    return None


def load_searches(path: Path | None = None) -> list[SearchEntry]:
    path = path or _first_existing("searches.yaml", "searches.example.yaml")
    if not path or not path.exists():
        return []
    raw = _read_structured(path) or []
    items = raw.get("searches", raw) if isinstance(raw, dict) else raw
    return [SearchEntry(**item) for item in items]


def load_employers(path: Path | None = None) -> list[Employer]:
    path = path or _first_existing("employers.yaml", "employers.example.yaml")
    if not path or not path.exists():
        return []
    raw = _read_structured(path) or []
    items = raw.get("employers", raw) if isinstance(raw, dict) else raw
    # Parse each entry independently: a single malformed employer must not abort
    # the whole load and silently disable ATS-board discovery for every other
    # company. Skip and warn on bad entries instead.
    employers: list[Employer] = []
    for item in items:
        try:
            employers.append(Employer(**item))
        except Exception as exc:  # noqa: BLE001 — one bad row shouldn't kill discovery
            from honestapply.logging_setup import get_logger

            name = item.get("name") if isinstance(item, dict) else item
            get_logger(__name__).warning(
                "config.bad_employer_entry", entry=name, error=str(exc)
            )
    return employers


def load_profile(path: Path | None = None) -> Profile:
    path = path or _first_existing("profile.json", "profile.example.json")
    if not path or not path.exists():
        return Profile()
    return Profile(**(_read_structured(path) or {}))


def load_answers(path: Path | None = None) -> dict[str, Any]:
    path = path or _first_existing("answers.yaml", "answers.example.yaml")
    if not path or not path.exists():
        return {}
    return _read_structured(path) or {}


def load_ats_selectors(path: Path | None = None) -> dict[str, Any]:
    path = path or _first_existing("ats_selectors.yaml")
    if not path or not path.exists():
        return {}
    return _read_structured(path) or {}
