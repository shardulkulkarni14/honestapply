"""Tests for apply-stage account auto-signup wiring (increment 3).

Offline: apply runs under the conftest mock (HONESTAPPLY_APPLY_MOCK=1), so no
browser/subprocess. Verifies the routing (account-walled → human when disabled,
→ proceeds when enabled) and the signup-mode helpers (proxied MCP config + env).
"""

from __future__ import annotations

import json

from honestapply.config import get_settings
from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.stages import apply as apply_mod

_SF_URL = "https://career.successfactors.eu/careers/job/12345"


def _covered_sf_job(add_job):
    return add_job(
        company="Walled Corp",
        title="AI Engineer",
        status=Status.COVERED,
        ats_type="successfactors",
        url=_SF_URL,
    )


def test_account_walled_routes_to_human_when_disabled(add_job):
    jid = _covered_sf_job(add_job)
    # default: honestapply_enable_account_signup is False
    apply_mod.run_apply(dry_run=True, limit=1)
    with session_scope() as s:
        assert s.get(Job, jid).status == Status.NEEDS_HUMAN


def test_account_walled_proceeds_when_signup_enabled(add_job, monkeypatch):
    jid = _covered_sf_job(add_job)
    monkeypatch.setenv("HONESTAPPLY_ENABLE_ACCOUNT_SIGNUP", "1")
    get_settings.cache_clear()
    assert get_settings().honestapply_enable_account_signup is True

    apply_mod.run_apply(dry_run=True, limit=1)
    with session_scope() as s:
        # signup mode took over instead of bailing to a human; the mock apply
        # completes the (first, forced) dry run.
        assert s.get(Job, jid).status == Status.DRY_RUN_COMPLETED


def test_signup_env_has_site_and_email():
    env = apply_mod._signup_env(_SF_URL)
    assert env["HONESTAPPLY_ACCOUNT_SITE"] == "career.successfactors.eu"
    assert "HONESTAPPLY_ACCOUNT_EMAIL" in env  # value comes from the profile


def test_signup_mcp_config_wraps_playwright_with_proxy():
    path = apply_mod._write_signup_mcp_config("test-signup-cfg")
    cfg = json.loads(open(path, encoding="utf-8").read())
    servers = cfg["mcpServers"]
    pw = servers["playwright"]
    # playwright now runs THROUGH the secret proxy, via this interpreter
    assert pw["args"][:2] == ["-m", "honestapply.secret_proxy"]
    assert "npx" in pw["args"]  # the real playwright command is wrapped inside
    # other servers (e.g. gmail, for email verification) are preserved as-is
    assert "gmail" in servers
    assert "secret_proxy" not in json.dumps(servers["gmail"])
