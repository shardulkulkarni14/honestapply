"""Which Gmail the agent subprocesses may read.

Two stages spawn a ``claude -p`` agent that needs to *read* mail: the apply stage
(one-time verification codes / magic links) and the inbox sync. By default they
use honestapply's **own** read-only Gmail MCP server (``gmail`` in ``.mcp.json``,
see ``docs/GMAIL_SETUP.md``). Its OAuth token can lapse, so as an **opt-in**
alternative the agent may use the Gmail connector of the Claude account that
``claude`` is logged into (``HONESTAPPLY_GMAIL_SOURCE=claude_connector``).

That connector is read-*write* (send, trash, labels, drafts …) and the agent runs
with permissions skipped, so connector mode is only allowed behind a hard
deny-list that leaves exactly three read tools. The list is built here, once per
run, from what the CLI actually reports (``claude mcp list``) plus a pinned set
of known tool names — never from the prompt alone.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable

PROJECT = "project"
CLAUDE_CONNECTOR = "claude_connector"
SOURCES = (PROJECT, CLAUDE_CONNECTOR)

# Tool-name prefix of the project's own server (``.mcp.json`` → ``gmail``).
PROJECT_GMAIL_SERVER = "gmail"
# Tool-name prefix the CLI gives the claude.ai Gmail connector.
CLAUDE_GMAIL_SERVER = "claude_ai_Gmail"

# The ONLY connector tools an agent may call: search and read. Nothing that
# writes to the mailbox, not even labels or drafts.
CLAUDE_GMAIL_READ_TOOLS: tuple[str, ...] = ("search_threads", "get_thread", "get_message")

# Every other tool the connector exposed when this was written (2026-11). Each is
# denied by name; a tool added later is still fenced by the prompt, and the
# server-level rules below keep every *other* connector out entirely.
CLAUDE_GMAIL_DENIED_TOOLS: tuple[str, ...] = (
    "apply_sensitive_message_label",
    "apply_sensitive_thread_label",
    "create_draft",
    "create_label",
    "delete_draft",
    "delete_label",
    "forward",
    "get_draft",
    "label_message",
    "label_thread",
    "list_drafts",
    "list_labels",
    "mark_message_spam",
    "mark_thread_spam",
    "reply",
    "send_message",
    "trash_message",
    "trash_thread",
    "unlabel_message",
    "unlabel_thread",
    "unmark_message_spam",
    "unmark_thread_spam",
    "untrash_message",
    "untrash_thread",
    "update_draft",
    "update_label",
    "update_message_labels",
)

# claude.ai connectors seen so far besides Gmail. Denied wholesale even when
# ``claude mcp list`` cannot be consulted.
KNOWN_OTHER_CONNECTORS: tuple[str, ...] = ("claude.ai Claude Docs", "claude.ai Indeed")

_CONNECTOR_LINE = re.compile(r"^(claude\.ai [^:]+):", re.MULTILINE)


def normalize_source(value: str | None) -> str:
    v = (value or PROJECT).strip().lower()
    if v not in SOURCES:
        raise ValueError(f"HONESTAPPLY_GMAIL_SOURCE must be one of {SOURCES}, got {value!r}")
    return v


def gmail_source(settings=None) -> str:
    """The configured source, validated."""
    if settings is None:
        from honestapply.config import get_settings

        settings = get_settings()
    return normalize_source(getattr(settings, "honestapply_gmail_source", PROJECT))


def connector_server_key(display_name: str) -> str:
    """``claude.ai Claude Docs`` → ``claude_ai_Claude_Docs`` (the ``mcp__<server>``
    key the CLI uses in tool names and permission rules)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", display_name.strip())


def list_claude_connectors(timeout: int = 60) -> list[str]:
    """Display names of the claude.ai connectors the CLI reports (``claude mcp
    list``). Empty on any failure — callers must treat that as "unknown", not
    "none", and fall back to the pinned list."""
    try:
        proc = subprocess.run(
            ["claude", "mcp", "list"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _CONNECTOR_LINE.findall(proc.stdout or "")


def claude_connector_disallowed_tools(connectors: Iterable[str] | None = None) -> list[str]:
    """Permission deny-rules for connector mode, sorted and de-duplicated:

    - the project's own ``gmail`` server (one Gmail path per run);
    - every claude.ai connector except Gmail, as a server-level rule;
    - every Gmail connector tool except the three read tools, by name.
    """
    if connectors is None:
        connectors = list_claude_connectors()
    names = {*KNOWN_OTHER_CONNECTORS, *connectors}
    rules = {f"mcp__{PROJECT_GMAIL_SERVER}"}
    for name in names:
        key = connector_server_key(name)
        if key != CLAUDE_GMAIL_SERVER:
            rules.add(f"mcp__{key}")
    for tool in CLAUDE_GMAIL_DENIED_TOOLS:
        rules.add(f"mcp__{CLAUDE_GMAIL_SERVER}__{tool}")
    return sorted(rules)


def claude_cli_args(source: str, *, mcp_config: str | None = None) -> list[str]:
    """The MCP/permission arguments for a ``claude -p`` agent run.

    ``project``: the given config is the *exact* server set (``--strict-mcp-config``).
    ``claude_connector``: strict mode off so the account connector is visible, and
    the deny-list from :func:`claude_connector_disallowed_tools` applied.
    """
    source = normalize_source(source)
    args: list[str] = []
    if mcp_config:
        args += ["--mcp-config", mcp_config]
    if source == PROJECT:
        if mcp_config:
            args.append("--strict-mcp-config")
        return args
    args += ["--disallowedTools", ",".join(claude_connector_disallowed_tools())]
    return args


def prompt_tools_paragraph(source: str) -> str:
    """The prompt text telling the agent which Gmail tools it may use."""
    source = normalize_source(source)
    if source == PROJECT:
        return (
            "Read mail using **only honestapply's own Gmail MCP server — the one named "
            "`gmail`** (its tools are prefixed `mcp__gmail__…`). Do **not** use any "
            "account-tied or built-in Gmail connector (e.g. a `claude_ai`/managed "
            "Gmail), even if one is available — this run must go through "
            "honestapply's own read-only connection. The server is authorized "
            "**read-only**, so no send/modify tools exist — that's expected."
        )
    reads = ", ".join(f"`mcp__{CLAUDE_GMAIL_SERVER}__{t}`" for t in CLAUDE_GMAIL_READ_TOOLS)
    return (
        "Read mail using **only** these three tools of the account's Gmail connector: "
        f"{reads}. They may be listed as deferred — load them the way this session "
        "loads deferred tools. Every other Gmail tool (send, reply, forward, drafts, "
        "labels, trash, spam) is disabled for this run; do not look for a way "
        "around that. Do **not** use the project's `gmail` server (`mcp__gmail__…`) "
        "in this run."
    )
