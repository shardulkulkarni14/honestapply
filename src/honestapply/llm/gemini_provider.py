"""Google Gemini provider for honestapply.

Uses the unified `google-genai` Python SDK (google.genai, >= 0.8).
The old `google-generativeai` SDK was deprecated November 2025.

API: client.models.generate_content(model, contents, config)
  config = types.GenerateContentConfig(
      system_instruction=..., temperature=..., max_output_tokens=...
  )
Response text: response.text
"""

from __future__ import annotations

from honestapply.llm.base import LLMError, LLMProvider
from honestapply.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_MODEL = "gemini-2.0-flash"


class GeminiProvider(LLMProvider):
    name = "gemini"

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
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise LLMError(
                "google-genai package not installed. Run: pip install google-genai"
            ) from exc

        client = genai.Client(api_key=self.api_key)

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        if system:
            config = types.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
                max_output_tokens=max_tokens,
            )

        try:
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            raise LLMError(f"Gemini API error: {exc}") from exc

        try:
            return response.text
        except AttributeError as exc:
            raise LLMError(f"Unexpected Gemini response structure: {exc}") from exc
