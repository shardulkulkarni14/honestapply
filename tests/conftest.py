"""Shared pytest fixtures: offline stub LLM, mock apply, isolated temp DB per test."""

from __future__ import annotations

import os

import pytest

# Set offline/mock env at import time — before any honestapply stage module is
# imported during collection — so providers default to stub and apply mocks out.
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("HONESTAPPLY_APPLY_MOCK", "1")


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Bind the global engine to a brand-new SQLite file for each test, and make
    settings hermetic.

    The suite must see the *shipped defaults* (safety guards, opt-in flags such as
    ``HONESTAPPLY_ENABLE_ACCOUNT_SIGNUP``), not whatever the developer has in the
    repo's gitignored ``.env`` or exported in their shell. Otherwise a personal
    opt-in silently flips safety-routing tests (account-walled → needs_human).
    """
    from honestapply.config import Settings, get_settings

    # 1. Don't read the repo's .env during tests.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    # 2. Drop any HONESTAPPLY_* knobs inherited from the shell; tests that need
    #    one set it explicitly (after this fixture, so their value wins).
    for key in list(os.environ):
        if key.startswith("HONESTAPPLY_"):
            monkeypatch.delenv(key, raising=False)

    db = tmp_path / "test.db"
    monkeypatch.setenv("HONESTAPPLY_DB_PATH", str(db))
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("HONESTAPPLY_APPLY_MOCK", "1")

    get_settings.cache_clear()
    from honestapply.db.session import init_db

    init_db(db)
    yield
    get_settings.cache_clear()


@pytest.fixture
def add_job():
    """Insert one Job and return its id."""
    from honestapply.db.models import Job, Status, url_hash
    from honestapply.db.session import session_scope

    def _add(**kw):
        url = kw.pop("url", "https://boards.greenhouse.io/acme/jobs/1")
        defaults = dict(
            company="Helix AI",
            title="Senior AI Engineer (LLM/RAG)",
            location="Munich, Germany",
            url=url,
            url_hash=url_hash(url + kw.get("company", "")),
            status=Status.ENRICHED,
            ats_type="greenhouse",
            description=(
                "Python, LLM, RAG, LangChain, LangGraph, FastAPI, Azure, pgvector, "
                "agentic systems, enterprise delivery."
            ),
        )
        defaults.update(kw)
        with session_scope() as s:
            j = Job(**defaults)
            s.add(j)
            s.flush()
            return j.id

    return _add
