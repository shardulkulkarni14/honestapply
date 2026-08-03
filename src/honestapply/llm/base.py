"""Provider-agnostic LLM interface + factory.

Concrete providers live in sibling modules (anthropic_provider, gemini_provider,
openai_provider) and subclass `LLMProvider`. A built-in `StubProvider` returns
deterministic canned output so the whole pipeline can run in `simulate` mode with
no API key, network, or cost.

Contract every provider implements:
    complete(prompt, *, system=None, max_tokens=2048, temperature=0.2) -> str
    complete_json(prompt, *, system=None, max_tokens=2048, temperature=0.2) -> dict
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from honestapply.config import Settings, get_settings


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    name: str = "base"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key
        self.model = model

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        """Return the model's text completion for `prompt`."""

    def complete_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Return parsed JSON. Tolerates code fences and surrounding prose."""
        raw = self.complete(
            prompt, system=system, max_tokens=max_tokens, temperature=temperature
        )
        try:
            return extract_json(raw)
        except LLMError:
            # One retry with an explicit correction. Providers occasionally answer
            # conversationally instead of emitting JSON (the `claude` CLI in
            # particular will sometimes reply "this looks like a prompt template,
            # not a task"), and a single such reply should not silently discard a
            # candidate that cost a network round-trip to enrich.
            retry = (
                f"{prompt}\n\n"
                "## OUTPUT FORMAT CORRECTION\n"
                "Your previous response was not valid JSON. Return ONLY the JSON "
                "object specified above — no prose, no questions, no code fences, "
                "no commentary. Start your response with '{' and end it with '}'."
            )
            raw = self.complete(
                retry, system=system, max_tokens=max_tokens, temperature=temperature
            )
            return extract_json(raw)


def extract_json(text: str) -> dict[str, Any]:
    """Best-effort: strip ```json fences, then grab the outermost {...}."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Could not parse JSON from LLM output: {exc}\n---\n{text[:500]}")
    raise LLMError(f"No JSON object found in LLM output:\n{text[:500]}")


class StubProvider(LLMProvider):
    """Deterministic, offline provider for tests and `simulate`.

    Heuristically detects which prompt it's answering (score / tailor / cover)
    and returns plausible structured output.
    """

    name = "stub"

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        p = prompt.lower()
        if "return only valid json" in p and "score" in p:
            return json.dumps(
                {
                    "score": 8,
                    "reasoning": "[stub] Strong overlap on core skills with minor seniority gap.",
                    "matched_keywords": ["python", "llm", "rag", "fastapi"],
                    "gap_flags": ["[stub] limited info on team size"],
                }
            )
        if "immutable facts" in p or "tailor" in p:
            # Echo back any provided YAML block unchanged (facts preserved),
            # else a minimal note. The tailor stage validates facts regardless.
            block = re.search(r"```(?:yaml)?\s*(.*?)```", prompt, re.DOTALL)
            if block:
                return f"```yaml\n{block.group(1).strip()}\n```"
            return "name: stub\nsummary_variants: ['[stub] tailored summary']\n"
        if "cover letter" in p:
            return (
                "Dear Hiring Manager,\n\n[stub] I am excited to apply for this role. "
                "My background in GenAI engineering and enterprise delivery aligns "
                "closely with your needs.\n\nSincerely,\n[stub applicant]"
            )
        return "[stub] response"


def get_provider(settings: Settings | None = None, provider: str | None = None) -> LLMProvider:
    """Instantiate the configured provider. Lazy-imports SDKs so missing
    optional deps don't break the rest of the CLI."""
    settings = settings or get_settings()
    name = (provider or settings.llm_provider).lower()

    if name == "stub":
        return StubProvider()

    if name == "claude_cli":
        from honestapply.llm.claude_cli_provider import ClaudeCliProvider

        return ClaudeCliProvider(model=settings.model_for(name))

    api_key = settings.api_key_for(name)
    model = settings.model_for(name)
    # A custom OpenAI-compatible endpoint may be a local server, which needs no
    # key — so only insist on one when talking to the vendor's own API.
    custom_endpoint = name == "openai" and bool(settings.openai_base_url)
    if not api_key and not custom_endpoint:
        raise LLMError(
            f"No API key for provider '{name}'. Set the matching *_API_KEY in .env, "
            f"or use LLM_PROVIDER=stub for offline runs."
        )

    if name == "anthropic":
        from honestapply.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=api_key, model=model)
    if name == "gemini":
        from honestapply.llm.gemini_provider import GeminiProvider

        return GeminiProvider(api_key=api_key, model=model)
    if name == "openai":
        from honestapply.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=api_key, model=model, base_url=settings.openai_base_url
        )
    raise LLMError(f"Unknown LLM provider: {name!r}")
