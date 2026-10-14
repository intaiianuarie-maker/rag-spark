"""LiteLLM provider — generate/stream via LiteLLM gateway / OpenRouter (T-ARCH-02).

Extends the single OpenRouter provider surface (OpenRouterProvider), preserving
full backward compatibility with LiteLLM gateway interfaces, contract tests,
and T-V1-029 local-only egress protection while adding:
- Automatic Gemma free-to-paid fallback on pool exhaustion
- Gemini-embedding-2 interface support
- Paid Gemini-generation lock
- Per-call accounting across all call sites
- Bounded retries with exponential backoff and loud exhaustion
"""

from __future__ import annotations

from typing import Any

from ..interfaces import LLMProvider
from .openrouter_provider import (
    DEFAULT_BASE_URL,
    DEFAULT_CHAT_FREE_MODEL,
    DEFAULT_CHAT_PAID_MODEL,
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_TIMEOUT,
    GLOBAL_ACCOUNTING,
    CallStats,
    EmbedderError,
    LLMProviderError,
    OpenRouterError,
    OpenRouterProvider,
    ProviderExhaustionError,
    ProviderPolicyError,
    create_openrouter_provider,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CHAT_FREE_MODEL",
    "DEFAULT_CHAT_PAID_MODEL",
    "DEFAULT_EMBEDDING_DIMENSION",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_TIMEOUT",
    "GLOBAL_ACCOUNTING",
    "CallStats",
    "EmbedderError",
    "LLMProviderError",
    "LiteLLMProvider",
    "OpenRouterError",
    "OpenRouterProvider",
    "ProviderExhaustionError",
    "ProviderPolicyError",
    "create_llm_provider",
    "create_openrouter_provider",
]


class LiteLLMProvider(OpenRouterProvider):
    """LiteLLM gateway / OpenRouter provider surface."""

    provider_name: str = "litellm"


def create_llm_provider(**kwargs: Any) -> LLMProvider:
    """Harness factory — for tests that need a real LiteLLM gateway."""
    return LiteLLMProvider(**kwargs)
