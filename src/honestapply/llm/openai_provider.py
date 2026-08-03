"""OpenAI provider for honestapply.

Uses the official `openai` Python SDK (>= 1.0).
API: client.chat.completions.create(model, messages, max_tokens, temperature)
Response text: completion.choices[0].message.content
"""

from __future__ import annotations

from honestapply.llm.base import LLMError, LLMProvider
from honestapply.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIProvider(LLMProvider):
    name = "openai"

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
            import openai
        except ImportError as exc:
            raise LLMError("openai package not installed. Run: pip install openai") from exc

        client = openai.OpenAI(api_key=self.api_key)

        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            completion = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except openai.APIError as exc:
            raise LLMError(f"OpenAI API error: {exc}") from exc

        try:
            content = completion.choices[0].message.content
            if content is None:
                raise LLMError("OpenAI returned an empty content response")
            return content
        except (IndexError, AttributeError) as exc:
            raise LLMError(f"Unexpected OpenAI response structure: {exc}") from exc
