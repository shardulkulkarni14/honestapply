"""Tests for the Playwright-MCP secret-injection proxy.

Unit-tests the pure substitute/redact helpers, then an end-to-end subprocess test
that wraps a fake MCP child and proves the no-leak contract: the real password
reaches the (fake) browser, but never appears on the agent-facing stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

from cryptography.fernet import Fernet

from honestapply import secret_proxy as sp
from honestapply import vault as V

TOKEN = sp.TOKEN


def test_substitute_only_inside_tools_call():
    secret = "S3cr3t!pw"
    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "browser_type", "arguments": {"text": TOKEN, "ref": "e5"}},
    }
    out = sp.substitute_outgoing((json.dumps(msg) + "\n").encode(), secret)
    got = json.loads(out)
    assert got["params"]["arguments"]["text"] == secret
    assert got["params"]["arguments"]["ref"] == "e5"


def test_substitute_leaves_non_tools_call_untouched():
    secret = "S3cr3t!pw"
    other = {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"note": TOKEN}}
    raw = (json.dumps(other) + "\n").encode()
    # the placeholder in a non-tools/call message is NOT substituted
    assert sp.substitute_outgoing(raw, secret) == raw


def test_substitute_ignores_non_json():
    secret = "S3cr3t!pw"
    raw = b"not json " + TOKEN.encode() + b"\n"
    assert sp.substitute_outgoing(raw, secret) == raw  # never inject into opaque lines


def test_redact_replaces_secret_with_token():
    secret = "S3cr3t!pw"
    raw = json.dumps({"result": {"value": secret}}).encode()
    out = sp.redact_incoming(raw, secret)
    assert secret.encode() not in out
    assert TOKEN.encode() in out


def test_end_to_end_secret_never_reaches_agent_stdout(tmp_path, monkeypatch):
    # Put BOTH this process and the proxy subprocess on the same file-backed vault
    # (same backend, key and dir) so they read the identical stored password.
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("HONESTAPPLY_VAULT_BACKEND", "file")
    monkeypatch.setenv("HONESTAPPLY_VAULT_KEY", key)
    monkeypatch.setenv("HONESTAPPLY_VAULT_DIR", str(tmp_path))
    env = dict(os.environ)
    env["HONESTAPPLY_ACCOUNT_SITE"] = "careers.acme.com"

    # Create the password and learn its value (the test is allowed to know it;
    # the proxy is what must never leak it to the agent channel).
    v = V.get_vault(config_dir=tmp_path)
    v.ensure_password("careers.acme.com", "me@example.com")
    secret = v.peek("careers.acme.com")
    assert secret

    # Fake MCP child: records exactly what it receives, then echoes the received
    # `text` value back out (simulating the browser returning a field's value).
    rx_file = tmp_path / "child_rx.txt"
    child = tmp_path / "fake_child.py"
    child.write_text(textwrap.dedent(f"""
        import sys, json
        rx = open({str(rx_file)!r}, "wb")
        for line in sys.stdin.buffer:
            rx.write(line); rx.flush()
            try:
                msg = json.loads(line)
                val = msg["params"]["arguments"]["text"]
            except Exception:
                continue
            out = json.dumps({{"jsonrpc":"2.0","id":msg.get("id"),"result":{{"value":val}}}})
            sys.stdout.write(out + "\\n"); sys.stdout.flush()
    """))

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "browser_type", "arguments": {"text": TOKEN}},
    }) + "\n"

    proc = subprocess.run(
        [sys.executable, "-m", "honestapply.secret_proxy", sys.executable, str(child)],
        input=request.encode(), capture_output=True, env=env, timeout=60,
    )

    agent_stdout = proc.stdout.decode()
    child_received = rx_file.read_text()

    # 1. Substitution worked: the real secret reached the (fake) browser.
    assert secret in child_received
    assert TOKEN not in child_received
    # 2. Redaction worked: the agent-facing stdout shows the token, never the secret.
    assert secret not in agent_stdout
    assert TOKEN in agent_stdout
