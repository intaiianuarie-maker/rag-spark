"""OpenAI-compatible HTTP adapter (chat + embeddings).

TASK-M7-001a: registry over HTTP (OpenAI-compatible, vLLM/Ollama/llama.cpp
contracts). No vendor SDK is imported here — only ``httpx``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from models.interfaces import (
    GenerationRequest,
    GenerationResult,
    LLMProvider,
)
from retrieval.interfaces import EmbeddingProvider

from ..registry import Capability, ProviderKind, ProviderSpec


@dataclass
class OpenAICompatProvider(LLMProvider, EmbeddingProvider):
    """A provider served over an OpenAI-compatible HTTP endpoint."""

    name: str
    base_url: str
    model: str
    embedding_dimension: int = 3072
    embedding_revision: str | None = None
    kind: ProviderKind = ProviderKind.LLM
    capabilities: Capability | frozenset[str] | tuple[str, ...] = field(default_factory=frozenset)
    cost: str = "low"
    latency: str = "medium"
    privacy_class: str = "cloud"
    local: bool = False
    api_key: str | None = None
    timeout: float = 30.0
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self.embedding_dimension <= 0:
            raise ValueError("embedding_dimension must be positive")
        if not self.embedding_revision:
            self.embedding_revision = self.model
        if isinstance(self.capabilities, (frozenset, set, tuple, list)):
            self.capabilities = frozenset(self.capabilities)
        self.spec = ProviderSpec(
            name=self.name,
            kind=self.kind,
            base_url=self.base_url,
            model=self.model,
            capabilities=self.capabilities,
            cost=self.cost,
            latency=self.latency,
            privacy_class=self.privacy_class,
            local=self.local,
            api_key=self.api_key,
            extra=dict(self.extra),
        )

    @property
    def model_name(self) -> str:  # type: ignore[override]
        return self.model

    @property
    def dimension(self) -> int:  # type: ignore[override]
        return self.embedding_dimension

    @property
    def revision(self) -> str:  # type: ignore[override]
        return self.embedding_revision or self.model

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client(self, client: httpx.AsyncClient | None) -> httpx.AsyncClient:
        if client is not None:
            return client
        return httpx.AsyncClient(timeout=self.timeout)

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        payload.update(kwargs)
        own = client is None
        c = self._client(client)
        try:
            resp = await c.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        finally:
            if own:
                await c.aclose()

    async def embed(
        self,
        inputs: str | Sequence[str],
        *,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ) -> list[list[float]]:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "input": inputs if isinstance(inputs, str) else list(inputs),
        }
        payload.update(kwargs)
        own = client is None
        c = self._client(client)
        try:
            resp = await c.post(
                f"{self.base_url}/embeddings",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            items = data["data"]
            if not isinstance(items, list):
                raise ValueError("embedding response missing data list")
            expected_count = 1 if isinstance(inputs, str) else len(inputs)
            if len(items) != expected_count:
                raise ValueError(
                    f"embedding count mismatch: got {len(items)}, expected {expected_count}"
                )

            has_index = [isinstance(item, dict) and "index" in item for item in items]
            if any(has_index) and not all(has_index):
                raise ValueError("mixed indexed and unindexed embedding entries")
            indexed = all(has_index)

            def _finite_vector(vec: Any, idx: int) -> list[float]:
                if not isinstance(vec, list):
                    raise ValueError(f"embedding {idx} is not a list")
                if len(vec) != self.embedding_dimension:
                    raise ValueError(
                        f"embedding {idx} length {len(vec)} != dimension {self.embedding_dimension}"
                    )
                out_vec: list[float] = []
                for value in vec:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise ValueError(f"embedding {idx} contains non-numeric value")
                    fval = float(value)
                    import math

                    if not math.isfinite(fval):
                        raise ValueError(f"embedding {idx} contains non-finite value")
                    out_vec.append(fval)
                return out_vec

            if indexed:
                indices: list[int] = []
                for item in items:
                    idx = item["index"]
                    if isinstance(idx, bool) or not isinstance(idx, int):
                        raise ValueError(f"invalid embedding index: {idx!r}")
                    if idx < 0 or idx >= len(items):
                        raise ValueError(f"embedding index out of range: {idx}")
                    indices.append(idx)
                if len(set(indices)) != len(indices):
                    raise ValueError("duplicate embedding indices")
                if sorted(indices) != list(range(len(items))):
                    raise ValueError("embedding indices are not an exact permutation")
                ordered: list[list[float] | None] = [None] * len(items)
                for item in items:
                    idx = item["index"]
                    ordered[idx] = _finite_vector(item["embedding"], idx)
                if any(v is None for v in ordered):
                    raise ValueError("embedding response missing indices")
                return [v for v in ordered if v is not None]

            out: list[list[float]] = []
            for idx, item in enumerate(items):
                out.append(_finite_vector(item["embedding"], idx))
            return out
        finally:
            if own:
                await c.aclose()

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        norm = []
        for m in request.messages:
            role = m.role.value if hasattr(m.role, "value") else str(m.role)
            norm.append({"role": role, "content": m.content})
        messages = norm
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": messages,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop
        client = None
        c = self._client(client)
        try:
            resp = await c.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            text = choice["message"]["content"]
            usage = data.get("usage") or {}
            return GenerationResult(
                text=text,
                model=data.get("model", request.model or self.model),
                finish_reason=choice.get("finish_reason"),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                metadata=dict(request.metadata or {}),
            )
        finally:
            await c.aclose()

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        norm = []
        for m in request.messages:
            role = m.role.value if hasattr(m.role, "value") else str(m.role)
            norm.append({"role": role, "content": m.content})
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": norm,
            "stream": True,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        client = None
        c = self._client(client)
        try:
            async with c.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:") :].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        import json

                        chunk = json.loads(data_str)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                    except Exception:
                        continue
        finally:
            await c.aclose()

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self.embed(list(texts))

    async def embed_query(self, text: str) -> list[float]:
        res = await self.embed([text])
        return res[0] if res else []
