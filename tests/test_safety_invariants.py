"""The safety rails as an executable contract.

The README tells strangers that the limits on this tool are constants in code
rather than settings — that config and environment cannot raise them. That claim
is only worth making if something enforces it, so these tests exist to fail the
build when a rail is weakened, rather than letting the change pass quietly and
the README become a lie.

Deliberately narrow: each test pins one promise made in public, and nothing else.
If a value here legitimately changes, the README and SECURITY.md must change in
the same commit.
"""

from __future__ import annotations

import inspect

import pytest

from honestapply.config import HARD_DAILY_CEILING, Settings


def test_hard_ceiling_value():
    """The published hard ceiling. Raising it is a decision about other people's
    inboxes as well as your own — see SECURITY.md."""
    assert HARD_DAILY_CEILING == 50


@pytest.mark.parametrize("attempted", [51, 100, 1000, 10**6])
def test_config_cannot_raise_the_hard_ceiling(attempted):
    """ACCEPTANCE: no configured daily cap can exceed the hard ceiling."""
    settings = Settings(honestapply_daily_cap=attempted)
    assert settings.effective_daily_cap == HARD_DAILY_CEILING


def test_environment_cannot_raise_the_hard_ceiling(monkeypatch):
    """Same promise, via the environment rather than the constructor."""
    monkeypatch.setenv("HONESTAPPLY_DAILY_CAP", "9999")
    assert Settings().effective_daily_cap == HARD_DAILY_CEILING


def test_a_lower_cap_is_still_respected():
    """The ceiling clamps; it must not become a floor."""
    assert Settings(honestapply_daily_cap=5).effective_daily_cap == 5


def test_published_safety_defaults():
    """The numbers quoted in the README's safety table."""
    s = Settings()
    assert s.honestapply_daily_cap == 25
    assert s.honestapply_dry_run_first_n == 3
    assert s.honestapply_rate_limit_seconds == 90
    assert s.honestapply_rate_limit_jitter_seconds == 30


def test_rails_are_checked_before_the_browser_agent_runs():
    """The rails must gate the subprocess, not live inside the agent's prompt.

    SECURITY.md leans on this ordering: an agent that misbehaves still cannot
    exceed the caps, because they are enforced before it is ever started.
    """
    from honestapply.stages import apply as apply_stage

    src = inspect.getsource(apply_stage)
    call = src.index("_run_claude(instructions_text)")
    for guard in ("dry_run_first_n", "effective_daily_cap", "rate_limit_seconds"):
        assert guard in src, f"{guard} guard missing entirely"
        assert src.index(guard) < call, f"{guard} is checked after the agent runs"


def test_linkedin_easy_apply_needs_two_explicit_optins():
    """Off by default, and not enabled by a single flag."""
    from honestapply.stages import apply as apply_stage

    src = inspect.getsource(apply_stage)
    assert "enable_linkedin_easy_apply" in src
    assert "I-UNDERSTAND-LINKEDIN-BAN-RISK" in src
