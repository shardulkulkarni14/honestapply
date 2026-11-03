"""Gmail source for the apply/inbox agents: project server (default) vs. the
Claude account's Gmail connector behind a hard deny-list.

Offline: inspects the generated CLI arguments, MCP config and prompt text; no
agent, browser or `claude mcp list` call is made (the connector listing is stubbed).
"""

from __future__ import annotations

import json

import pytest

from honestapply import gmail_source as gs
from honestapply.email import agent as inbox_agent
from honestapply.stages import apply as apply_mod

READ = {f"mcp__{gs.CLAUDE_GMAIL_SERVER}__{t}" for t in gs.CLAUDE_GMAIL_READ_TOOLS}


@pytest.fixture(autouse=True)
def _no_cli(monkeypatch):
    monkeypatch.setattr(
        gs,
        "list_claude_connectors",
        lambda timeout=60: ["claude.ai Gmail", "claude.ai Claude Docs", "claude.ai Indeed"],
    )


def test_pinned_tool_lists_are_disjoint_and_complete():
    assert not set(gs.CLAUDE_GMAIL_READ_TOOLS) & set(gs.CLAUDE_GMAIL_DENIED_TOOLS)
    assert len(gs.CLAUDE_GMAIL_READ_TOOLS) == 3
    # every mutating tool we know of is denied by name
    for must in (
        "send_message",
        "reply",
        "forward",
        "trash_thread",
        "create_draft",
        "label_thread",
    ):
        assert must in gs.CLAUDE_GMAIL_DENIED_TOOLS


def test_default_source_is_project_and_strict():
    assert gs.normalize_source(None) == gs.PROJECT
    assert gs.claude_cli_args(gs.PROJECT, mcp_config="/tmp/x.json") == [
        "--mcp-config",
        "/tmp/x.json",
        "--strict-mcp-config",
    ]
    assert gs.claude_cli_args(gs.PROJECT) == []


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError):
        gs.normalize_source("all_the_mail")


def test_connector_deny_list_fences_everything_but_three_reads():
    rules = set(
        gs.claude_connector_disallowed_tools(
            ["claude.ai Gmail", "claude.ai Claude Docs", "claude.ai Something New"]
        )
    )
    # the project's own server and every other connector are denied at server level
    assert "mcp__gmail" in rules
    assert "mcp__claude_ai_Claude_Docs" in rules
    assert "mcp__claude_ai_Something_New" in rules
    assert "mcp__claude_ai_Indeed" in rules  # pinned even if the listing omits it
    # the Gmail connector itself is NOT denied wholesale …
    assert f"mcp__{gs.CLAUDE_GMAIL_SERVER}" not in rules
    # … only every tool except the reads
    denied_gmail = {r for r in rules if r.startswith(f"mcp__{gs.CLAUDE_GMAIL_SERVER}__")}
    assert denied_gmail == {
        f"mcp__{gs.CLAUDE_GMAIL_SERVER}__{t}" for t in gs.CLAUDE_GMAIL_DENIED_TOOLS
    }
    assert not denied_gmail & READ


def test_connector_listing_failure_still_denies_known_connectors(monkeypatch):
    monkeypatch.setattr(gs, "list_claude_connectors", lambda timeout=60: [])
    rules = set(gs.claude_connector_disallowed_tools())
    assert {"mcp__gmail", "mcp__claude_ai_Claude_Docs", "mcp__claude_ai_Indeed"} <= rules


def test_connector_cli_args_drop_strict_and_pass_one_deny_argument():
    args = gs.claude_cli_args(gs.CLAUDE_CONNECTOR, mcp_config="/tmp/x.json")
    assert "--strict-mcp-config" not in args
    assert args[:2] == ["--mcp-config", "/tmp/x.json"]
    i = args.index("--disallowedTools")
    rules = args[i + 1].split(",")
    assert "mcp__gmail" in rules and f"mcp__{gs.CLAUDE_GMAIL_SERVER}__send_message" in rules
    assert not set(rules) & READ


def test_apply_run_claude_command_per_source(monkeypatch):
    seen: list[list[str]] = []

    class _Proc:
        stdout = '<<<RESULT>>>{"status": "failed", "reason": "stub"}<<<END>>>'
        stderr = ""

    monkeypatch.setattr(apply_mod.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _Proc())
    apply_mod._run_claude("x", mcp_config="/tmp/c.json")
    apply_mod._run_claude("x", mcp_config="/tmp/c.json", gmail_source=gs.CLAUDE_CONNECTOR)
    default, connector = seen
    assert "--strict-mcp-config" in default and "--disallowedTools" not in default
    assert "--strict-mcp-config" not in connector
    deny = connector[connector.index("--disallowedTools") + 1].split(",")
    assert "mcp__gmail" in deny and not set(deny) & READ
    assert connector[-2:] == ["-p", "x"]


def test_connector_mode_drops_project_gmail_from_run_config():
    path = apply_mod._build_apply_mcp_config(
        "test-gmail-src", drop_servers=(gs.PROJECT_GMAIL_SERVER,)
    )
    servers = json.loads(open(path, encoding="utf-8").read())["mcpServers"]
    assert "gmail" not in servers and "playwright" in servers


def test_prompts_name_the_right_tools():
    proj = gs.prompt_tools_paragraph(gs.PROJECT)
    conn = gs.prompt_tools_paragraph(gs.CLAUDE_CONNECTOR)
    assert "mcp__gmail__" in proj and "claude_ai_Gmail" not in proj
    for t in gs.CLAUDE_GMAIL_READ_TOOLS:
        assert f"mcp__{gs.CLAUDE_GMAIL_SERVER}__{t}" in conn
    assert "send" in conn and "disabled" in conn

    text = inbox_agent.render_instructions(
        candidates="[]",
        seen_ids="[]",
        lookback_days=7,
        max_results=10,
        gmail_source=gs.CLAUDE_CONNECTOR,
    )
    assert f"mcp__{gs.CLAUDE_GMAIL_SERVER}__get_thread" in text and "{gmail_tools}" not in text
    text = inbox_agent.render_instructions(
        candidates="[]", seen_ids="[]", lookback_days=7, max_results=10
    )
    assert "mcp__gmail__" in text


def test_settings_knob_reaches_the_launcher(monkeypatch):
    from honestapply.config import get_settings

    monkeypatch.setenv("HONESTAPPLY_GMAIL_SOURCE", "claude_connector")
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    assert gs.gmail_source(get_settings()) == gs.CLAUDE_CONNECTOR
    monkeypatch.setenv("HONESTAPPLY_GMAIL_SOURCE", "project")
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    assert gs.gmail_source(get_settings()) == gs.PROJECT
