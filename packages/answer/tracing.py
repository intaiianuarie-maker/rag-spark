"""Per-call answer diagnostics, captured at existing retrieval/provider seams.

The engine and its dependencies are never mutated. Each call uses a dataclass
copy with forwarding observers, so concurrent workspaces cannot share trace
state. No prompts, generated text or source content enter the trace.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import wraps
from time import perf_counter
from typing import Any

from core.auth import ACLScope
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider
from models.usage import StreamUsage, capture_stream_usage
from retrieval.interfaces import RetrievalMode, RetrievedItem

from .models import Answer
from .prompts import SYSTEM_PROMPT


@dataclass
class _Trace:
    query: str
    scope: ACLScope
    mode: RetrievalMode
    started: float = field(default_factory=perf_counter)
    retrieval_ms: float | None = None
    retrieval_status: str = "not_run"
    retrieval_stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    generation: dict[str, Any] = field(default_factory=lambda: {"status": "not_run"})
    generation_finished: float | None = None
    rewrite: dict[str, Any] = field(default_factory=lambda: {"status": "not_reported"})

    def payload(self, answer: Answer) -> dict[str, Any]:
        finished = perf_counter()
        return {
            "version": 1,
            "query": {
                "sha256": hashlib.sha256(self.query.encode()).hexdigest(),
                "characters": len(self.query),
            },
            "rewrite": dict(self.rewrite),
            "filters": {
                "workspace_ids": list(self.scope.workspace_ids),
                "include_public": self.scope.include_public,
                "mode": self.mode.value,
            },
            "retrieval": {
                "status": self.retrieval_status,
                "latency_ms": self.retrieval_ms,
                "candidate_scope": "returned_context_only",
                "stages": deepcopy(self.retrieval_stages),
                "items": self.items,
            },
            "context": {
                "chunk_ids": [item["chunk_id"] for item in self.items if item["chunk_id"]],
                "source_text_included": False,
            },
            "generation": dict(self.generation),
            "verification": {
                "phase": "parse_verify_publish",
                "status": answer.publication_status or answer.status.value,
                "latency_ms": (
                    (finished - self.generation_finished) * 1000
                    if self.generation_finished is not None
                    else None
                ),
                "usage_status": "not_reported",
            },
            "total_latency_ms": (finished - self.started) * 1000,
        }


def _finite_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


class _RetrievalObserver:
    def __init__(self, delegate: Any, trace: _Trace) -> None:
        self.delegate = delegate
        self.trace = trace

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def retrieve(self, **kwargs: Any) -> list[RetrievedItem]:
        from retrieval.orchestrator import retrieval_outcome_ctx

        # A delegate that does not publish diagnostics cannot inherit an older
        # call's outcome on this task. The engine still owns consuming the new one.
        retrieval_outcome_ctx.set(None)
        started = perf_counter()
        forwarded = kwargs.get("query")
        if isinstance(forwarded, str):
            self.trace.rewrite = {
                "status": "unchanged" if forwarded == self.trace.query else "rewritten",
                "forwarded_query_sha256": hashlib.sha256(forwarded.encode()).hexdigest(),
                "forwarded_query_characters": len(forwarded),
                "observed_at": "retrieval_input",
            }
        try:
            items = await self.delegate.retrieve(**kwargs)
        except BaseException:
            self.trace.retrieval_status = "failed"
            raise
        finally:
            self.trace.retrieval_ms = (perf_counter() - started) * 1000
        self.trace.retrieval_status = "completed"
        # Observe without consuming the outcome; the engine still owns pop().
        outcome = retrieval_outcome_ctx.get()
        if outcome is not None:
            self.trace.retrieval_status = outcome.status.value
            self.trace.retrieval_stages = deepcopy(outcome.stages)
        # Defense in depth: diagnostics never expose an unscoped item even if
        # a faulty test double/adapter returns it. The retrieval behavior itself
        # is forwarded unchanged; the engine's existing guards remain in force.
        self.trace.items = [
            {
                "chunk_id": item.chunk_id,
                "document_id": item.document_id,
                "workspace_id": item.workspace_id,
                "score": _finite_score(item.score),
                "scores": {
                    key: score
                    for key, value in item.scores.items()
                    if (score := _finite_score(value)) is not None
                },
            }
            for item in items
            if self.trace.scope.allows_workspace(item.workspace_id)
        ]
        # A hydration wrapper may drop stale canonical rows after the base
        # orchestrator measured its stages. Count only the final scoped items
        # this observer actually forwards; operation status/count/timing stays
        # the measured upstream evidence, never inferred from surviving scores.
        for stage, key in (("dense", "dense"), ("sparse", "sparse"),
                           ("lexical", "lexical"), ("graph", "graph"),
                           ("fusion", "rrf"), ("rerank", "rerank")):
            if stage in self.trace.retrieval_stages:
                self.trace.retrieval_stages[stage]["returned_context_count"] = sum(
                    1 for item in self.trace.items if key in item["scores"]
                )
        if "context" in self.trace.retrieval_stages:
            self.trace.retrieval_stages["context"]["returned_context_count"] = len(self.trace.items)
        return items


class _ProviderObserver(LLMProvider):
    def __init__(self, delegate: LLMProvider, trace: _Trace) -> None:
        self.delegate = delegate
        self.trace = trace
        self.provider_name = getattr(delegate, "provider_name", "unreported")

    def _start(self, request: GenerationRequest) -> float:
        self.trace.generation = {
            "status": "running",
            "model": request.model,
            "requested_model": request.model,
            "prompt_template_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "prompt_tokens": None,
            "completion_tokens": None,
            "usage_status": "not_reported",
        }
        return perf_counter()

    def _finish(self, started: float) -> None:
        self.trace.generation_finished = perf_counter()
        self.trace.generation["latency_ms"] = (self.trace.generation_finished - started) * 1000

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        started = self._start(request)
        try:
            result = await self.delegate.generate(request)
            self.trace.generation.update(
                {
                    "status": "completed",
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "usage_status": (
                        "reported"
                        if result.prompt_tokens is not None and result.completion_tokens is not None
                        else "not_reported"
                    ),
                }
            )
            return result
        except BaseException:
            self.trace.generation["status"] = "failed"
            raise
        finally:
            self._finish(started)

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        started = self._start(request)
        usage = StreamUsage()
        tokens = None
        try:
            with capture_stream_usage(usage):
                tokens = self.delegate.stream(request)
            while True:
                with capture_stream_usage(usage):
                    try:
                        token = await anext(tokens)
                    except StopAsyncIteration:
                        break
                yield token
            self.trace.generation["status"] = "completed"
        except BaseException:
            self.trace.generation["status"] = "failed"
            raise
        finally:
            try:
                close = getattr(tokens, "aclose", None)
                if close is not None:
                    with capture_stream_usage(usage):
                        await close()
            finally:
                self.trace.generation.update({
                    "model": usage.model or request.model,
                    "model_status": usage.model_status,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "cost_usd": usage.cost_usd,
                    "usage_status": usage.usage_status,
                })
                self._finish(started)


def _observed(engine: Any, trace: _Trace) -> Any:
    return replace(
        engine,
        orchestrator=_RetrievalObserver(engine.orchestrator, trace),
        llm=_ProviderObserver(engine.llm, trace),
    )


def trace_answer(fn: Callable[..., Awaitable[Answer]]) -> Callable[..., Awaitable[Answer]]:
    @wraps(fn)
    async def call(
        engine: Any, *, query: str, acl_scope: ACLScope, mode: RetrievalMode = RetrievalMode.HYBRID
    ) -> Answer:
        trace = _Trace(query=query, scope=acl_scope, mode=mode)
        answer = await fn(_observed(engine, trace), query=query, acl_scope=acl_scope, mode=mode)
        answer.debug_trace = trace.payload(answer)
        return answer

    return call


def trace_stream(
    fn: Callable[..., AsyncIterator[tuple[str, Any]]],
) -> Callable[..., AsyncIterator[tuple[str, Any]]]:
    @wraps(fn)
    async def call(
        engine: Any, *, query: str, acl_scope: ACLScope, mode: RetrievalMode = RetrievalMode.HYBRID
    ) -> AsyncIterator[tuple[str, Any]]:
        trace = _Trace(query=query, scope=acl_scope, mode=mode)
        events = fn(_observed(engine, trace), query=query, acl_scope=acl_scope, mode=mode)
        try:
            async for kind, value in events:
                if kind == "verified" and isinstance(value, Answer):
                    value.debug_trace = trace.payload(value)
                yield kind, value
        finally:
            close = getattr(events, "aclose", None)
            if close is not None:
                await close()

    return call
