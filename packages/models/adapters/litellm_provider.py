"""LiteLLM provider — generate/stream via LiteLLM gateway (MIT).

Reuse: LiteLLM MIT (service, see REUSE_MAP.md) + OpenAI-compatible API.
Adapter speaks LiteLLM's OpenAI-compatible /chat/completions via httpx;
no LiteLLM type crosses the boundary — we return GenerationResult/str.

Design: base_url from LITELLM_URL (default http://localhost:4000),
api_key from LITELLM_API_KEY (optional, for gateway auth). Model identity
is passthrough per contract; empty model raises typed error.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..interfaces import ChatMessage, GenerationRequest, GenerationResult, LLMProvider

DEFAULT_BASE_URL = "http://localhost:4000"
DEFAULT_TIMEOUT = 30.0


class LLMProviderError(RuntimeError):
    """Typed error for LiteLLM gateway failures."""


def _to_openai_messages(msgs: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in msgs:
        d: dict[str, Any] = {"role": m.role.value, "content": m.content}
        if m.name:
            d["name"] = m.name
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        out.append(d)
    return out


def _trim_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Trim the gateway body to what the RAG debugger needs (review Minor #2).

    The full response body is large and mostly redundant with the typed fields
    already on :class:`GenerationResult`; shipping it verbatim bloated every
    result. Keep only the identifying fields.
    """
    return {
        "raw": {
            "id": data.get("id"),
            "created": data.get("created"),
            "model": data.get("model"),
            "system_fingerprint": data.get("system_fingerprint"),
        }
    }


class LiteLLMProvider(LLMProvider):
    """LiteLLM gateway — OpenAI-compatible chat completions."""

    provider_name = "litellm"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("LITELLM_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("LITELLM_API_KEY")
        self.timeout = timeout
        #: Injected in tests; otherwise one client is created lazily and reused
        #: (review Minor #1 — no per-call client churn).
        self._client = client
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"content-type": "application/json"}
        if self.api_key:
            h["authorization"] = f"Bearer {self.api_key}"
        return h

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, headers=self._headers())
        return self._client

    async def aclose(self) -> None:
        """Close the internally-owned client (injected clients are left alone)."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if not request.model.strip():
            raise LLMProviderError("model name must not be empty")
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": _to_openai_messages(request.messages),
            "stream": False,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop

        client = self._http()
        try:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            snippet = e.response.text[:500]
            raise LLMProviderError(
                f"LiteLLM error {e.response.status_code}: {snippet}"
            ) from e
        except Exception as e:
            raise LLMProviderError(f"LiteLLM request failed: {e}") from e

        try:
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or choice.get("text") or ""
            usage = data.get("usage") or {}
            return GenerationResult(
                text=str(text),
                model=request.model,
                finish_reason=choice.get("finish_reason"),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                metadata=_trim_metadata(data),
            )
        except Exception as e:
            raise LLMProviderError(f"LiteLLM response parse failed: {e}") from e

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        if not request.model.strip():
            raise LLMProviderError("model name must not be empty")
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": _to_openai_messages(request.messages),
            "stream": True,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop

        client = self._http()
        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as resp:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    raise LLMProviderError(f"LiteLLM stream error {e.response.status_code}") from e
                async for line in resp.aiter_lines():
                    if not line.strip() or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                        chunk = delta.get("content")
                        if chunk:
                            yield str(chunk)
                    except Exception:
                        continue
        except LLMProviderError:
            raise
        except Exception as e:
            raise LLMProviderError(f"LiteLLM stream failed: {e}") from e


def create_llm_provider(**kwargs: Any) -> LLMProvider:
    """Harness factory — for tests that need a real LiteLLM gateway."""
    return LiteLLMProvider(**kwargs)
