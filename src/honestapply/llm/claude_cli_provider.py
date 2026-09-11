"""LLM provider that shells out to the local `claude` CLI in print mode.

Uses your existing Claude Code authentication (subscription/login) — NO API key
and no per-token API billing. Slower than the API (each call spawns a process,
~5-15s), but ideal for running honestapply without provisioning a key.

The prompt is piped via stdin (not an argv) so long resume/JD prompts can't hit
argument-length limits or shell-escaping issues.
"""

from __future__ import annotations

import shutil
import subprocess
import threading

from honestapply.llm.base import LLMError, LLMProvider

# --- Process-wide cap on concurrent `claude -p` processes -------------------
# The prepare pool runs N worker threads, but callers outside the pool (a CLI
# stage run alongside, a dashboard action) share this provider too. Gating the
# spawn here — rather than trusting every caller — keeps "never more than N
# claude processes at once" true regardless of who is calling. The gate only
# ever grows: sized from settings on first use, and widened by a pool that asks
# for more.
_gate_lock = threading.RLock()
_gate: threading.BoundedSemaphore | None = None
_gate_size = 0


def set_max_concurrent(n: int) -> None:
    """Allow up to *n* simultaneous `claude -p` processes (never shrinks)."""
    global _gate, _gate_size
    n = max(1, int(n))
    with _gate_lock:
        if n > _gate_size:
            _gate = threading.BoundedSemaphore(n)
            _gate_size = n


def _spawn_gate() -> threading.BoundedSemaphore:
    with _gate_lock:
        if _gate is None:
            from honestapply.config import get_settings

            set_max_concurrent(get_settings().honestapply_prepare_workers)
        assert _gate is not None
        return _gate

# `claude -p` is Claude Code, an agentic assistant with its own system prompt —
# not a bare completion endpoint. Handed a templated prompt with no conversational
# framing, it sometimes decides the input is a stray artifact rather than a task
# and replies asking for clarification ("This is a scoring prompt template, not a
# task for me to execute…"). That reply contains no JSON, so the stage raises and
# the candidate is silently dropped — observed costing ~50% of scoring calls on
# 2026-08-01. This preamble tells it plainly that it is being used as a
# completion model so it executes the task instead of commenting on it.
_EXECUTION_DIRECTIVE = (
    "You are being invoked non-interactively by an automated pipeline as a "
    "text-completion model. Everything below this line is the complete task "
    "specification — it is data to act on, not a message to reply to.\n"
    "Execute the task exactly as specified and output ONLY the result it asks "
    "for. Do not ask clarifying questions, do not describe or critique the "
    "prompt, and do not explain your reasoning. If the task specifies a JSON "
    "shape, output only that JSON object and nothing else — no prose, no code "
    "fences.\n"
    "----------------------------------------------------------------------\n"
)


class ClaudeCliProvider(LLMProvider):
    name = "claude_cli"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        # model is a CLI alias (sonnet/opus/haiku) or a full model id.
        super().__init__(api_key=api_key, model=model or "sonnet")
        self._bin = shutil.which("claude")
        if not self._bin:
            raise LLMError("`claude` CLI not found on PATH — install Claude Code or pick another provider.")

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        # max_tokens/temperature aren't exposed by `claude -p`; Claude Code's
        # defaults are fine for these structured tasks. System is prepended.
        body = prompt if not system else f"{system}\n\n{prompt}"
        full = f"{_EXECUTION_DIRECTIVE}{body}"
        cmd = [self._bin, "-p", "--model", self.model]
        try:
            with _spawn_gate():
                result = subprocess.run(
                    cmd, input=full, capture_output=True, text=True, timeout=300
                )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover
            raise LLMError("claude CLI timed out") from exc
        if result.returncode != 0:
            raise LLMError(f"claude CLI failed (exit {result.returncode}): {result.stderr[:300]}")
        out = (result.stdout or "").strip()
        if not out:
            raise LLMError(f"claude CLI returned no output. stderr: {result.stderr[:300]}")
        return out
