"""Apply-stage wiring for human-in-the-loop notifications.

Offline: inspects the generated MCP config and the NOTIFY marker parsing; no
agent/browser is spawned.
"""

from __future__ import annotations

import json

from honestapply.stages import apply as apply_mod


def test_signup_mcp_config_wraps_playwright_with_proxy():
    path = apply_mod._write_signup_mcp_config("test-notify-cfg")
    servers = json.loads(open(path, encoding="utf-8").read())["mcpServers"]
    pw = servers["playwright"]
    assert pw["args"][:2] == ["-m", "honestapply.secret_proxy"]
    assert "npx" in pw["args"]  # the real playwright command is wrapped inside
    assert "gmail" in servers  # other servers preserved


def test_notify_regex_matches_notify_not_result():
    notify_line = '<<<NOTIFY>>>{"type": "captcha", "reason": "solve it"}<<<END>>>'
    m = apply_mod._NOTIFY_RE.search(notify_line)
    assert m and json.loads(m.group(1))["type"] == "captcha"
    # a RESULT block is not a NOTIFY (different opener)
    assert apply_mod._NOTIFY_RE.search('<<<RESULT>>>{"status": "applied"}<<<END>>>') is None


def test_parse_claude_result_takes_last_block():
    out = (
        '<<<NOTIFY>>>{"type":"captcha"}<<<END>>>\n'
        '<<<RESULT>>>{"status": "applied", "reason": "ok"}<<<END>>>\n'
    )
    assert apply_mod._parse_claude_result(out)["status"] == "applied"
