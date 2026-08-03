"""Any OpenAI-compatible endpoint, including local ones.

`POST /v1/chat/completions` is the de facto standard, so a configurable base_url
is the cheapest possible way to support Ollama, LM Studio, llama.cpp, vLLM,
OpenRouter, Groq, Together and DeepSeek. These tests pin the two behaviours that
make that work: the URL reaches the client, and a local endpoint does not demand
an API key it has no use for.
"""

from __future__ import annotations

import pytest

from honestapply.config import Settings
from honestapply.llm.base import LLMError, get_provider
from honestapply.llm.openai_provider import OpenAIProvider

OLLAMA = "http://localhost:11434/v1"


def test_provider_carries_the_base_url():
    assert OpenAIProvider(api_key="k", base_url=OLLAMA).base_url == OLLAMA


def test_default_base_url_is_none_so_openai_itself_still_works():
    """No base_url configured must mean 'talk to OpenAI', not 'talk to nothing'."""
    assert OpenAIProvider(api_key="k").base_url is None
    assert Settings().openai_base_url is None


def test_factory_passes_the_configured_endpoint_through():
    settings = Settings(
        llm_provider="openai", openai_api_key="k", openai_base_url=OLLAMA
    )
    assert get_provider(settings).base_url == OLLAMA


def test_local_endpoint_needs_no_api_key():
    """ACCEPTANCE: a local server has no key, and must not be blocked for lacking one."""
    settings = Settings(
        llm_provider="openai", openai_api_key=None, openai_base_url=OLLAMA
    )
    provider = get_provider(settings)  # must not raise
    assert provider.base_url == OLLAMA


def test_openai_proper_still_requires_a_key():
    """The relaxation must apply only to custom endpoints."""
    settings = Settings(llm_provider="openai", openai_api_key=None, openai_base_url=None)
    with pytest.raises(LLMError, match="No API key"):
        get_provider(settings)


def test_other_providers_are_unaffected_by_the_relaxation():
    settings = Settings(llm_provider="anthropic", anthropic_api_key=None)
    with pytest.raises(LLMError, match="No API key"):
        get_provider(settings)
