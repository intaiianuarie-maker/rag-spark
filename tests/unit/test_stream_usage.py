"""Real SSE metadata stays scoped to its provider/trace call, without network."""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from answer.tracing import _ProviderObserver, _RetrievalObserver, _Trace
from core.auth import ACLScope
from models.adapters.openrouter_provider import (
    DEFAULT_CHAT_FREE_MODEL,
    DEFAULT_CHAT_PAID_MODEL,
    GLOBAL_ACCOUNTING,
    LLMProviderError,
    OpenRouterProvider,
)
from models.interfaces import GenerationRequest, LLMProvider
from models.usage import StreamUsage, capture_stream_usage, stream_usage_ctx
from retrieval.interfaces import RetrievalMode

pytestmark = pytest.mark.unit


def _trace():
    return _Trace(query="private question", scope=ACLScope(workspace_ids=["A"]),
                  mode=RetrievalMode.HYBRID)


def _sse(*frames):
    return ": OPENROUTER PROCESSING\n\n" + "".join(
        f"data: {json.dumps(frame)}\n\n" for frame in frames
    ) + "data: [DONE]\n\n"


def _chunk(model="actual-model", content="private output"):
    return {"model": model, "choices": [{"delta": {"content": content}}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("choices", [[], [{"delta": {"content": ""}, "finish_reason": "stop"}]])
async def test_final_usage_frames_reach_trace_and_accounting(choices):
    bodies = []
    GLOBAL_ACCOUNTING.reset("usage-frame")

    async def handle(request):
        bodies.append(json.loads(request.content))
        await asyncio.sleep(0)
        return httpx.Response(200, text=_sse(_chunk(), {
            "model": "actual-model", "choices": choices,
            "usage": {"prompt_tokens": 123, "completion_tokens": 17,
                      "total_tokens": 140, "cost": 0.0007},
        }))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        trace = _trace()
        provider = OpenRouterProvider(client=client, api_key="fixture-key", call_site="usage-frame")
        observer = _ProviderObserver(provider, trace)
        request = GenerationRequest(model="requested-model")
        with capture_stream_usage(StreamUsage()) as outer:
            assert [part async for part in observer.stream(request)] == ["private output"]
            assert stream_usage_ctx.get() is outer
            assert outer.model is None
        assert stream_usage_ctx.get() is None
    generation = trace.generation
    assert generation["usage_status"] == "reported"
    assert generation["prompt_tokens"] == 123
    assert generation["completion_tokens"] == 17
    assert generation["total_tokens"] == 140
    assert generation["cost_usd"] == 0.0007
    assert generation["model"] == "actual-model"
    assert generation["model_status"] == "reported"
    assert generation["requested_model"] == "requested-model"
    assert "private output" not in json.dumps(generation)
    assert "fixture-key" not in json.dumps(generation)
    assert bodies[0]["stream_options"] == {"include_usage": True}
    stats = GLOBAL_ACCOUNTING.summary()["sites"]["usage-frame"]
    assert stats["prompt_tokens"] == 123
    assert stats["completion_tokens"] == 17
    assert stats["cost_usd"] == 0.0007
    assert stats["unreported_usage_calls"] == 0


@pytest.mark.asyncio
async def test_missing_usage_stays_unknown_and_never_uses_chunk_estimates():
    async def handle(request):
        return httpx.Response(200, text=_sse(_chunk(), _chunk(content="more")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        trace = _trace()
        observer = _ProviderObserver(
            OpenRouterProvider(client=client, call_site="missing-stream"), trace
        )
        parts = [part async for part in observer.stream(GenerationRequest(model="unknown"))]
        assert len(parts) == 2
    assert trace.generation["prompt_tokens"] is None
    assert trace.generation["completion_tokens"] is None
    assert trace.generation["cost_usd"] is None
    assert trace.generation["usage_status"] == "not_reported"
    stats = GLOBAL_ACCOUNTING.summary()["sites"]["missing-stream"]
    assert stats["prompt_tokens"] == stats["completion_tokens"] == 0  # sum of known values only
    assert stats["unreported_usage_calls"] >= 1
    assert stats["cost_is_metered"] is False


@pytest.mark.asyncio
async def test_reported_tokens_without_upstream_cost_are_not_metered_cost():
    site = "stream-missing-cost"
    GLOBAL_ACCOUNTING.reset(site)

    async def handle(request):
        if json.loads(request.content)["model"] == DEFAULT_CHAT_FREE_MODEL:
            return httpx.Response(429)
        return httpx.Response(200, text=_sse(_chunk(model=DEFAULT_CHAT_PAID_MODEL), {
            "usage": {"prompt_tokens": 120, "completion_tokens": 30}, "choices": [],
        }))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        trace = _trace()
        provider = OpenRouterProvider(client=client, call_site=site)
        observer = _ProviderObserver(provider, trace)
        request = GenerationRequest(model=DEFAULT_CHAT_FREE_MODEL)
        _ = [part async for part in observer.stream(request)]
    assert trace.generation["usage_status"] == "reported"
    assert trace.generation["cost_usd"] is None
    assert trace.generation["model"] == DEFAULT_CHAT_PAID_MODEL
    stats = GLOBAL_ACCOUNTING.summary()["sites"][site]
    assert stats["prompt_tokens"] == 120
    assert stats["completion_tokens"] == 30
    assert stats["cost_usd"] == 0  # sum of reported costs only
    assert stats["cost_is_metered"] is False
    assert GLOBAL_ACCOUNTING.summary()["total"]["cost_is_metered"] is False


@pytest.mark.asyncio
async def test_paid_fallback_reports_selected_model_even_without_sse_model():
    models = []

    async def handle(request):
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == DEFAULT_CHAT_FREE_MODEL:
            return httpx.Response(429)
        return httpx.Response(200, text=_sse({"choices": [{"delta": {"content": "paid"}}]}, {
            "usage": {"prompt_tokens": 11, "completion_tokens": 2}, "choices": [],
        }))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        trace = _trace()
        observer = _ProviderObserver(OpenRouterProvider(client=client), trace)
        request = GenerationRequest(model=DEFAULT_CHAT_FREE_MODEL)
        assert [part async for part in observer.stream(request)] == ["paid"]
    assert models == [DEFAULT_CHAT_FREE_MODEL, DEFAULT_CHAT_PAID_MODEL]
    assert trace.generation["model"] == DEFAULT_CHAT_PAID_MODEL
    assert trace.generation["model_status"] == "selected"
    assert trace.generation["prompt_tokens"] == 11


class ConcurrentProvider(LLMProvider):
    async def generate(self, request):
        raise NotImplementedError

    async def stream(self, request):
        usage = stream_usage_ctx.get()
        await asyncio.sleep(0)
        usage.observe({"model": request.model, "usage": {
            "prompt_tokens": 10 if request.model == "A" else 20, "completion_tokens": 0,
        }})
        yield request.model
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_concurrent_calls_and_same_task_interleaving_do_not_share_collectors():
    provider = ConcurrentProvider()
    traces = [_trace(), _trace()]
    streams = [_ProviderObserver(provider, trace).stream(GenerationRequest(model=model))
               for trace, model in zip(traces, ("A", "B"), strict=True)]
    # Same-task interleaving is stricter than separate ContextVar task copies:
    # metadata bindings must not remain installed while a caller handles text.
    assert await anext(streams[0]) == "A"
    assert stream_usage_ctx.get() is None
    assert await anext(streams[1]) == "B"
    assert stream_usage_ctx.get() is None
    for stream in reversed(streams):
        await stream.aclose()
    assert [trace.generation["prompt_tokens"] for trace in traces] == [10, 20]

    async def consume(model):
        trace = _trace()
        observer = _ProviderObserver(provider, trace)
        assert [part async for part in observer.stream(GenerationRequest(model=model))] == [model]
        assert stream_usage_ctx.get() is None
        return trace.generation

    results = await asyncio.gather(consume("A"), consume("B"))
    assert [result["prompt_tokens"] for result in results] == [10, 20]


@pytest.mark.asyncio
async def test_parallel_http_streams_through_shared_provider_capture_their_own_usage():
    async def handle(request):
        model = json.loads(request.content)["model"]
        await asyncio.sleep(0)
        return httpx.Response(200, text=_sse(_chunk(model=model), {
            "model": model, "usage": {
                "prompt_tokens": 7 if model == "A" else 19, "completion_tokens": 3,
            }, "choices": [],
        }))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OpenRouterProvider(client=client)

        async def consume(model):
            trace = _trace()
            observer = _ProviderObserver(provider, trace)
            _ = [part async for part in observer.stream(GenerationRequest(model=model))]
            assert stream_usage_ctx.get() is None
            return trace.generation

        results = await asyncio.gather(consume("A"), consume("B"))
    assert [result["model"] for result in results] == ["A", "B"]
    assert [result["prompt_tokens"] for result in results] == [7, 19]


@pytest.mark.asyncio
async def test_sse_error_is_failed_and_does_not_leak_upstream_message():
    async def handle(request):
        return httpx.Response(200, text=_sse({"model": "actual", "error": {
            "message": "private upstream message"}, "choices": []}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        trace = _trace()
        observer = _ProviderObserver(OpenRouterProvider(client=client), trace)
        with pytest.raises(LLMProviderError, match="reported an error") as caught:
            _ = [part async for part in observer.stream(GenerationRequest(model="requested"))]
        assert "private upstream" not in str(caught.value)
    assert stream_usage_ctx.get() is None
    assert trace.generation["status"] == "failed"
    assert trace.generation["prompt_tokens"] is None


@pytest.mark.asyncio
async def test_cancelled_provider_resets_context_and_finishes_trace():
    entered, closed = asyncio.Event(), asyncio.Event()

    class WaitingProvider(ConcurrentProvider):
        async def stream(self, request):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield "never"
            finally:
                closed.set()

    trace = _trace()
    observer = _ProviderObserver(WaitingProvider(), trace)

    async def consume():
        try:
            return [part async for part in observer.stream(GenerationRequest(model="A"))]
        finally:
            assert stream_usage_ctx.get() is None

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert trace.generation["status"] == "failed"
    assert trace.generation["latency_ms"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forwarded,status", [("private question", "unchanged"), ("another", "rewritten")]
)
async def test_rewrite_reports_only_the_observed_retrieval_input_hash(forwarded, status):
    class Retriever:
        async def retrieve(self, **kwargs):
            return []

    trace = _trace()
    await _RetrievalObserver(Retriever(), trace).retrieve(query=forwarded)
    assert trace.rewrite["status"] == status
    assert trace.rewrite["forwarded_query_sha256"] == hashlib.sha256(forwarded.encode()).hexdigest()
    assert forwarded not in json.dumps(trace.rewrite)


@pytest.mark.parametrize("bad", [-1, True, "5", 1.5, None])
def test_invalid_usage_values_are_not_reported_as_tokens(bad):
    usage = StreamUsage()
    usage.observe({"usage": {"prompt_tokens": bad, "completion_tokens": bad}})
    assert usage.prompt_tokens is usage.completion_tokens is None
    assert usage.usage_status == "not_reported"
