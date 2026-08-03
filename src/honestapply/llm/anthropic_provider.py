"""Anthropic Claude provider for honestapply.

Uses the official `anthropic` Python SDK (>= 0.40).
API: client.messages.create(model, max_tokens, messages, system, temperature)
Response text: message.content[0].text
"""

from __future__ import annotations

from honestapply.llm.base import LLMError, LLMProvider
from honestapply.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        super().__init__(api_key=api_key, model=model or DEFAULT_MODEL)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError("anthropic package not installed. Run: pip install anthropic") from exc

        client = anthropic.Anthropic(api_key=self.api_key)

        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        if system:
            kwargs["system"] = system

        try:
            message = client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic API error: {exc}") from exc

        try:
            return message.content[0].text
        except (IndexError, AttributeError) as exc:
            raise LLMError(f"Unexpected Anthropic response structure: {exc}") from exc
