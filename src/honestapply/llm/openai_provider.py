"""OpenAI-compatible provider for honestapply.

Uses the official `openai` Python SDK (>= 1.0), but is not limited to OpenAI.
`POST /v1/chat/completions` is the de facto standard, so pointing `base_url`
elsewhere reaches Ollama, LM Studio, llama.cpp's server, vLLM, OpenRouter, Groq,
Together, DeepSeek, Fireworks and others through this one class — including fully
local models, where no key is required and none is sent.

    OPENAI_BASE_URL=http://localhost:11434/v1   OPENAI_MODEL=qwen3:14b   # Ollama
    OPENAI_BASE_URL=https://openrouter.ai/api/v1                         # OpenRouter

A caution worth repeating from SECURITY.md: the immutable-facts validator makes a
weak model's fabrication *fail loudly* during tailoring, but cover letters have no
equivalent hard check. Small local models over-claim more, and more quietly.

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

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(api_key=api_key, model=model or DEFAULT_MODEL)
        self.base_url = base_url

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

        # Local servers (Ollama, LM Studio, llama.cpp) ignore the key but the SDK
        # insists on a non-empty one, so send a placeholder rather than failing.
        client = openai.OpenAI(
            api_key=self.api_key or "not-needed",
            base_url=self.base_url or None,
        )

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
