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

    # Paths
    honestapply_db_path: str = "data/honestapply.db"
    honestapply_browser_profile: str = "~/.honestapply/browser-profile"

    # Safety knobs
    honestapply_rate_limit_seconds: int = 90
    honestapply_rate_limit_jitter_seconds: int = 30
    honestapply_daily_cap: int = 25
    honestapply_min_score: int = 7
    honestapply_dry_run_first_n: int = 3

    # --- Derived helpers ---
    @property
    def effective_daily_cap(self) -> int:
        return min(self.honestapply_daily_cap, HARD_DAILY_CEILING)

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
    """A company whose ATS board we scrape directly via its public read API."""

    model_config = ConfigDict(extra="allow")
    name: str
    ats: Literal["greenhouse", "lever", "ashby", "smartrecruiters", "workday"]
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

    def summary_for_scoring(self) -> str:
        name = " ".join(
            str(self.legal_name.get(k, "")) for k in ("first", "last")
        ).strip()
        locs = ", ".join(self.preferred_locations)
        return (
            f"Name: {name}\n"
            f"Preferred locations: {locs}\n"
            f"Work authorization: {self.work_authorization}\n"
            f"Salary expectation: {self.salary_expectation}\n"
        )


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
    return [Employer(**item) for item in items]


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
