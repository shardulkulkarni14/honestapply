"""Secret-injection proxy for the Playwright MCP server (apply-stage auto-signup).

Runs as a stdio MCP server that the apply agent connects to, and transparently
relays JSON-RPC to the *real* Playwright MCP it wraps:

    python -m honestapply.secret_proxy npx @playwright/mcp@latest

Its one job is to let the agent fill an account password **without ever seeing
it**. The agent types the literal placeholder ``{{honestapply_password}}`` into a
password field; this proxy:

  - **substitutes** the placeholder with the real password (from the local vault,
    keyed by ``HONESTAPPLY_ACCOUNT_SITE``) in the agent→browser direction, so the
    real value reaches the page but never the agent; and
  - **redacts** any occurrence of that password in the browser→agent direction, so
    the agent can't read it back (e.g. via ``browser_evaluate`` on the field).

When ``HONESTAPPLY_ACCOUNT_SITE`` is unset it is a transparent pass-through. The
password is held only in memory here, is never written to logs, and the proxy
prints nothing of its own to the JSON-RPC stdout channel.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

TOKEN = "{{honestapply_password}}"


def _load_secret() -> str | None:
    """The account password for this run, or None in pass-through mode."""
    site = os.environ.get("HONESTAPPLY_ACCOUNT_SITE", "").strip()
    if not site:
        return None
    try:
        from honestapply.vault import get_vault

        vault = get_vault()
        vault.ensure_password(site, os.environ.get("HONESTAPPLY_ACCOUNT_EMAIL", ""))
        return vault.peek(site)
    except Exception:  # noqa: BLE001 — never crash the relay over vault issues
        return None


def _deep_replace(obj: object, token: str, value: str) -> None:
    """In-place replace *token* with *value* in every string within *obj*."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                if token in v:
                    obj[k] = v.replace(token, value)
            else:
                _deep_replace(v, token, value)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                if token in v:
                    obj[i] = v.replace(token, value)
            else:
                _deep_replace(v, token, value)


def substitute_outgoing(raw: bytes, secret: str) -> bytes:
    """agent→browser: swap the placeholder for the real secret, but only inside a
    `tools/call` message's params. Non-JSON / other messages pass through."""
    if TOKEN.encode() not in raw:
        return raw
    try:
        msg = json.loads(raw)
    except Exception:  # noqa: BLE001
        return raw  # not JSON — never inject a secret into an opaque line
    if isinstance(msg, dict) and msg.get("method") == "tools/call":
        _deep_replace(msg.get("params"), TOKEN, secret)
        return (json.dumps(msg) + "\n").encode()
    return raw


def redact_incoming(raw: bytes, secret: str) -> bytes:
    """browser→agent: replace any occurrence of the real secret with the
    placeholder, so the agent can never read the password back."""
    if not secret:
        return raw
    return raw.replace(secret.encode(), TOKEN.encode())


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        sys.stderr.write("secret_proxy: no wrapped command given\n")
        return 2

    child = subprocess.Popen(  # noqa: S603 — args come from our own .mcp config
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,  # inherit: child logs go to our stderr, never stdout
        bufsize=0,
    )

    # Resolve the secret once, lazily and cached, so pass-through mode never
    # touches the vault.
    _cache: dict[str, str | None] = {}

    def secret() -> str | None:
        if "v" not in _cache:
            _cache["v"] = _load_secret()
        return _cache["v"]

    def pump_up() -> None:
        assert child.stdin is not None
        for raw in sys.stdin.buffer:
            s = secret()
            out = substitute_outgoing(raw, s) if s else raw
            child.stdin.write(out)
            child.stdin.flush()
        try:
            child.stdin.close()
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=pump_up, daemon=True)
    t.start()

    # browser→agent in the main thread.
    assert child.stdout is not None
    out = sys.stdout.buffer
    for raw in child.stdout:
        out.write(redact_incoming(raw, secret() or ""))
        out.flush()

    return child.wait()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
