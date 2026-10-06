"""LLM provider contracts (docs/architecture/module-boundaries.md).

Reuse (`docs/reuse/V1_COMPONENTS.md`): standard model APIs only. Future adapters
(NOT wired in M0, no API keys/models):

- OpenAI-compatible HTTP APIs
- local vLLM / Ollama / llama.cpp servers via their OpenAI-compatible endpoints

Vendor SDK types must not cross this boundary; adapters return our dataclasses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatMessage(BaseModel):
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None


class GenerationRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class GenerationResult(BaseModel):
    text: str
    model: str
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class LLMProvider(ABC):
    """Adapter boundary for chat/generation models.

    The RAG path (M4) calls ``generate``/``stream``; citations are resolved by
    our API from evidence ids, never invented by the model.
    """

    provider_name: str = ""

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        raise NotImplementedError

    @abstractmethod
    def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        """Yield response text chunks (stub signature; not implemented in M0)."""
        raise NotImplementedError
