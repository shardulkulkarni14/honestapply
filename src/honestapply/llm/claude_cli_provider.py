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

from honestapply.llm.base import LLMError, LLMProvider


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
