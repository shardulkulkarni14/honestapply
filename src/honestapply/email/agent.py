"""Run the Gmail triage agent — a `claude` subprocess that uses Gmail MCP tools.

honestapply is an agentic tool, so the inbox is driven the same way the apply
stage is: we do **not** call the Gmail API from Python. Instead we spawn a
``claude -p`` session that has a **Gmail MCP server** available (configured in
``.mcp.json`` beside Playwright) and let the agent read mail via MCP *tool calls*
(``search_threads`` / ``get_thread`` / …). The agent prints a single
``<<<RESULT>>> … <<<END>>>`` JSON block, exactly like the browser-apply agent.

This module only *runs* the agent and *parses* its result. Every honest guard —
confidence floor, forward-only / no-regress transitions, ``source='email'``
attribution, company-mention sanity check, dedup — lives in ``sync.py`` and is
applied to the agent's output. Google's Gmail MCP server has no send tool, so the
agent can read and (later) draft, but can never transmit mail.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from honestapply.logging_setup import get_logger

log = get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "inbox_triage.md"

# The LAST result block wins, same convention as the apply stage.
_RESULT_RE = re.compile(r"<<<RESULT>>>\s*(\{.*?\})\s*<<<END>>>", re.DOTALL)

# Env hooks, mirroring the apply stage (HONESTAPPLY_APPLY_MOCK / _MODEL).
_MOCK_ENV = "HONESTAPPLY_INBOX_MOCK"
_MODEL_ENV = "HONESTAPPLY_INBOX_MODEL"
_DEFAULT_MODEL = "claude-opus-5"

# An agent runner takes the rendered instructions and returns the parsed result
# dict ({"messages": [...]}) — injectable so tests and sim never spawn a process.
AgentRunner = Callable[[str], dict[str, Any]]


class InboxAgentError(RuntimeError):
    """The triage agent could not be run, or returned no parseable result."""


def is_mock() -> bool:
    # Read at call time so sim/tests that set the env var after import still take
    # the mock path.
    return bool(os.environ.get(_MOCK_ENV, ""))


def mock_runner(_instructions: str) -> dict[str, Any]:
    """Offline stand-in: finds nothing. Used when HONESTAPPLY_INBOX_MOCK is set."""
    return {"messages": []}


def render_instructions(
    *,
    candidates: str,
    seen_ids: str,
    lookback_days: int,
    max_results: int,
) -> str:
    """Fill the triage prompt. Plain token replacement (not str.format) so the
    JSON example braces in the template are left untouched — same approach the
    apply stage uses for apply_browser.md."""
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    subs = {
        "candidates": candidates,
        "seen_ids": seen_ids,
        "lookback_days": str(lookback_days),
        "max_results": str(max_results),
    }
    for key, value in subs.items():
        template = template.replace("{" + key + "}", value)
    return template


def run_triage_agent(instructions: str, *, timeout: int | None = None) -> dict[str, Any]:
    """Spawn ``claude -p <instructions>`` (Gmail MCP available via .mcp.json) and
    return the parsed result dict. Raises :class:`InboxAgentError` on failure.

    Your email is **never written to disk**: the prompt forbids the agent from
    downloading/saving anything, and — belt and braces — the Gmail MCP server's
    download/attachment directories are jailed to a throwaway temp dir that is
    wiped when the run ends, so even an attempted save can't land in your home."""
    from honestapply.config import get_settings

    if timeout is None:
        timeout = getattr(get_settings(), "honestapply_inbox_timeout_seconds", 600)
    model = os.environ.get(_MODEL_ENV, _DEFAULT_MODEL)

    jail = tempfile.mkdtemp(prefix="honestapply-gmail-")
    env = {
        **os.environ,
        # @klodr/gmail-mcp path jails — any download/attachment write is confined
        # here, and this dir is removed below, so nothing is left on disk.
        "GMAIL_MCP_DOWNLOAD_DIR": jail,
        "GMAIL_MCP_ATTACHMENT_DIR": jail,
    }
    try:
        proc = subprocess.run(
            ["claude", "--dangerously-skip-permissions", "--model", model, "-p", instructions],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise InboxAgentError(
            "The `claude` CLI is not on PATH. The inbox stage runs an agent that "
            "reads Gmail over MCP, so it needs Claude Code installed."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise InboxAgentError(f"Gmail triage agent timed out after {timeout}s.") from exc
    finally:
        shutil.rmtree(jail, ignore_errors=True)

    return parse_result(proc.stdout or "")


def parse_result(stdout: str) -> dict[str, Any]:
    """Pull the last <<<RESULT>>> JSON block out of the agent's stdout."""
    matches = _RESULT_RE.findall(stdout)
    if not matches:
        tail = stdout[-1500:] if len(stdout) > 1500 else stdout
        raise InboxAgentError(
            "The triage agent produced no <<<RESULT>>> block. This usually means "
            "the Gmail MCP server isn't connected — run `honestapply gmail-connect` "
            f"and check `honestapply doctor`.\nLast agent output:\n{tail}"
        )
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        raise InboxAgentError(f"Could not parse the agent's result JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InboxAgentError("Agent result was not a JSON object.")
    return data


def default_runner() -> AgentRunner:
    """The runner to use when the caller didn't inject one: the real agent, or
    the offline mock when HONESTAPPLY_INBOX_MOCK is set."""
    return mock_runner if is_mock() else run_triage_agent
