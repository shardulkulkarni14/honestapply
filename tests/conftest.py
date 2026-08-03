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
    """Bind the global engine to a brand-new SQLite file for each test."""
    db = tmp_path / "test.db"
    monkeypatch.setenv("HONESTAPPLY_DB_PATH", str(db))
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("HONESTAPPLY_APPLY_MOCK", "1")

    from honestapply.config import get_settings

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
