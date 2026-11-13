"""Opt-in steering knobs: title exclusion, salary-stated bonus, per-host gate."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from honestapply.stages.score import states_salary


def _reload_settings(monkeypatch, **env):
    from honestapply.config import get_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


class _FakeProvider:
    """Returns a fixed score so the gate arithmetic is tested, not the stub."""

    def __init__(self, score: int, gap_flags: list[str] | None = None):
        self._result = {
            "score": score,
            "reasoning": "fits",
            "matched_keywords": [],
            "gap_flags": gap_flags or [],
        }

    def complete_json(self, prompt: str) -> dict:
        return dict(self._result)


def _score(jid: int, provider: _FakeProvider, threshold: int = 7):
    from honestapply.db.models import Job
    from honestapply.db.session import session_scope
    from honestapply.stages.score import _score_one

    with session_scope() as s:
        job = s.get(Job, jid)
        _score_one(job, provider, "profile", threshold)
        return job.score, job.status, job.score_reasoning, job.status_reason


# --- salary detector ---------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Salary: €70,000 – €90,000 depending on experience.",
        "Gehalt: 70.000 € brutto pro Jahr",
        "We offer 70k–90k EUR plus equity.",
        "Compensation range 70-100k.",
        "Jahresgehalt zwischen 65.000 und 80.000 Euro; Vergütung ab 65.000 EUR.",
        "Base pay $120,000 to $150,000 per year",
        "The annual salary for this role is £65,000.",
        "EUR 80 000 - 95 000",
    ],
)
def test_states_salary_positive(text):
    assert states_salary(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Competitive salary and a great team.",
        "We raised €5M in our seed round.",
        "We have raised $40 million from top investors.",
        "Backed by €500,000 in funding, we are growing fast.",
        "Our revenue grew to €120,000,000 last year.",
        "Series A of $12,000,000 – $15,000,000 closed in spring.",
        "5-10 years of experience, team of 20-30 engineers.",
        "Office at 80331 Munich; learning budget of €1,500 per year.",
        "You will handle 50k requests per second.",
    ],
)
def test_states_salary_negative(text):
    assert states_salary(text) is False


# --- salary bonus ------------------------------------------------------------
def test_bonus_off_by_default(add_job):
    jid = add_job(description="Python. Salary: €70,000 – €90,000.")
    score, _, reasoning, _ = _score(jid, _FakeProvider(7))
    assert score == 7 and "salary stated" not in reasoning


def test_bonus_lifts_score_over_the_gate(add_job, monkeypatch):
    from honestapply.db.models import Status

    _reload_settings(monkeypatch, HONESTAPPLY_SALARY_STATED_BONUS="1")
    jid = add_job(description="Python. Salary: €70,000 – €90,000.")
    score, status, reasoning, _ = _score(jid, _FakeProvider(7), threshold=8)
    assert score == 8 and status == Status.SCORED
    assert reasoning.endswith("(+1: salary stated)")


def test_bonus_needs_a_stated_salary(add_job, monkeypatch):
    _reload_settings(monkeypatch, HONESTAPPLY_SALARY_STATED_BONUS="1")
    jid = add_job(description="Python. Competitive salary.")
    assert _score(jid, _FakeProvider(7))[0] == 7


def test_bonus_is_capped_at_ten(add_job, monkeypatch):
    _reload_settings(monkeypatch, HONESTAPPLY_SALARY_STATED_BONUS="3")
    jid = add_job(description="Salary: €70,000 – €90,000.")
    score, _, reasoning, _ = _score(jid, _FakeProvider(9))
    assert score == 10 and "(+1: salary stated)" in reasoning


def test_bonus_never_lifts_a_language_capped_score(add_job, monkeypatch):
    from honestapply.db.models import Status

    _reload_settings(monkeypatch, HONESTAPPLY_SALARY_STATED_BONUS="5")
    jid = add_job(description="Salary: €70,000 – €90,000. German C1 required.")
    score, status, reasoning, _ = _score(jid, _FakeProvider(4, ["german_requirement"]), 7)
    assert score == 4 and status == Status.SKIPPED_LOW_FIT
    assert "salary stated" not in reasoning


# --- per-host gate -----------------------------------------------------------
def test_host_rule_raises_the_gate(add_job, monkeypatch):
    from honestapply.db.models import Status

    _reload_settings(monkeypatch, HONESTAPPLY_MIN_SCORE_BY_HOST="Greenhouse.io=9, other.example=11")
    on_host = add_job(url="https://boards.greenhouse.io/acme/jobs/1")
    elsewhere = add_job(url="https://jobs.ashbyhq.com/acme/2", company="Other Co")
    score, status, _, reason = _score(on_host, _FakeProvider(8))
    assert score == 8 and status == Status.SKIPPED_LOW_FIT and reason == "score 8 < min 9"
    assert _score(elsewhere, _FakeProvider(8))[1] == Status.SCORED


def test_host_rule_never_lowers_the_gate(monkeypatch):
    settings = _reload_settings(monkeypatch, HONESTAPPLY_MIN_SCORE_BY_HOST="greenhouse.io=3")
    assert settings.min_score_for("https://boards.greenhouse.io/acme/jobs/1") == 7
    assert settings.min_score_for("https://boards.greenhouse.io/acme/jobs/1", 9) == 9
    assert settings.min_score_for(None) == 7


def test_run_score_applies_host_rule(add_job, monkeypatch):
    from honestapply.db.models import Job, Status
    from honestapply.db.session import session_scope
    from honestapply.stages.score import run_score

    _reload_settings(monkeypatch, HONESTAPPLY_MIN_SCORE_BY_HOST="greenhouse.io=9")
    jid = add_job()  # greenhouse URL; the stub scores 8
    run_score()
    with session_scope() as s:
        assert s.get(Job, jid).status == Status.SKIPPED_LOW_FIT


@pytest.mark.parametrize("raw", ["greenhouse.io", "greenhouse.io=high", "=9", "a=1,b"])
def test_malformed_host_rule_fails_loudly(monkeypatch, raw):
    with pytest.raises(ValueError, match="HONESTAPPLY_MIN_SCORE_BY_HOST"):
        _reload_settings(monkeypatch, HONESTAPPLY_MIN_SCORE_BY_HOST=raw)


# --- title exclusion ---------------------------------------------------------
def test_invalid_title_regex_fails_loudly(monkeypatch):
    with pytest.raises(ValueError, match="HONESTAPPLY_EXCLUDE_TITLE_REGEX"):
        _reload_settings(monkeypatch, HONESTAPPLY_EXCLUDE_TITLE_REGEX="(senior")


def test_prefilter_drops_excluded_titles(add_job, monkeypatch):
    from honestapply.db.models import Job, Status
    from honestapply.db.session import session_scope
    from honestapply.stages.prefilter import run_prefilter

    _reload_settings(monkeypatch, HONESTAPPLY_EXCLUDE_TITLE_REGEX=r"\b(senior|principal)\b")
    dropped = add_job(title="SENIOR AI Engineer")
    kept = add_job(title="AI Engineer", url="https://jobs.ashbyhq.com/acme/2")
    assert run_prefilter()["dropped"] == 1
    with session_scope() as s:
        assert s.get(Job, dropped).status == Status.SKIPPED_LOW_FIT
        assert "HONESTAPPLY_EXCLUDE_TITLE_REGEX" in s.get(Job, dropped).status_reason
        assert s.get(Job, kept).status == Status.ENRICHED


def test_prefilter_keeps_everything_when_unset(add_job):
    from honestapply.stages.prefilter import run_prefilter

    add_job(title="Senior AI Engineer")
    assert run_prefilter()["dropped"] == 0


def _run_picker(monkeypatch, capsys) -> set[str]:
    path = Path(__file__).resolve().parent.parent / "scripts" / "pick_candidates.py"
    spec = importlib.util.spec_from_file_location("pick_candidates_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The liveness probe is a real HTTP call; tests never touch the network.
    monkeypatch.setattr(module, "_filter_live", lambda rows, limit: rows[:limit])
    monkeypatch.setattr(sys, "argv", ["pick_candidates.py", "--country", "DE"])
    module.main()
    return {x for x in capsys.readouterr().out.strip().split(",") if x}


def test_picker_skips_excluded_titles_and_unreachable_hosts(add_job, monkeypatch, capsys):
    _reload_settings(
        monkeypatch,
        HONESTAPPLY_EXCLUDE_TITLE_REGEX=r"\bsenior\b",
        HONESTAPPLY_MIN_SCORE_BY_HOST="greenhouse.io=9,never.example=11",
    )
    plain = add_job(title="AI Engineer", url="https://jobs.ashbyhq.com/a/1", company="A")
    add_job(title="Senior AI Engineer", url="https://jobs.ashbyhq.com/b/1", company="B")
    unscored = add_job(title="AI Engineer", url="https://boards.greenhouse.io/c/1", company="C")
    add_job(title="AI Engineer", url="https://boards.greenhouse.io/d/1", company="D", score=8)
    passing = add_job(title="AI Engineer", url="https://boards.greenhouse.io/e/1", company="E",
                      score=9)
    add_job(title="AI Engineer", url="https://never.example/f/1", company="F")
    assert _run_picker(monkeypatch, capsys) == {str(plain), str(unscored), str(passing)}


def test_picker_unchanged_when_settings_unset(add_job, monkeypatch, capsys):
    a = add_job(title="Senior AI Engineer", url="https://boards.greenhouse.io/a/1", score=7)
    assert _run_picker(monkeypatch, capsys) == {str(a)}
