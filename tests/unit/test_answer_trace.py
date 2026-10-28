"""Diagnostics observe real engine execution without source-text disclosure."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from answer.engine import AnswerEngine
from answer.models import Answer
from core.auth import ACLScope
from models.interfaces import GenerationResult, LLMProvider
from retrieval.interfaces import RetrievedItem


class FixtureRetriever:
    async def retrieve(self, *, query, acl_scope, **kwargs):
        await asyncio.sleep(0)  # exercise concurrent calls through one engine
        workspace = acl_scope.workspace_ids[0]
        return [
            RetrievedItem(
                chunk_id=f"chunk-{workspace}",
                document_id=f"doc-{workspace}",
                workspace_id=workspace,
                content=f"Private evidence for {workspace}.",
                score=0.8,
                scores={"dense": 0.7, "lexical": 0.6, "rrf": 0.8, "rerank": 0.9},
            )
        ]


class FixtureProvider(LLMProvider):
    provider_name = "fixture"

    def __init__(self):
        self.closed = False

    async def generate(self, request):
        await asyncio.sleep(0)
        workspace = "A" if "chunk-A" in request.messages[-1].content else "B"
        text = f"Private evidence for {workspace}."
        return GenerationResult(
            model=request.model,
            prompt_tokens=12,
            completion_tokens=8,
            text=json.dumps(
                {
                    "answer": text,
                    "claims": [
                        {"text": text, "chunk_ids": [f"chunk-{workspace}"]},
                    ],
                }
            ),
        )

    async def stream(self, request):
        try:
            result = await self.generate(request)
            for i in range(0, len(result.text), 8):
                yield result.text[i : i + 8]
        finally:
            self.closed = True


@pytest.mark.asyncio
async def test_parallel_answers_trace_their_own_scope_scores_and_usage():
    retrieval, provider = FixtureRetriever(), FixtureProvider()
    engine = AnswerEngine(orchestrator=retrieval, llm=provider, model="fixture-model")
    answers = await asyncio.gather(
        *[
            engine.answer(query=f"Question for {ws}?", acl_scope=ACLScope(workspace_ids=[ws]))
            for ws in ("A", "B")
        ]
    )
    for ws, answer in zip(("A", "B"), answers, strict=True):
        trace = answer.debug_trace
        assert trace is not None
        assert trace["filters"]["workspace_ids"] == [ws]
        assert trace["context"]["chunk_ids"] == [f"chunk-{ws}"]
        assert trace["context"]["source_text_included"] is False
        assert trace["retrieval"]["items"][0]["scores"] == {
            "dense": 0.7,
            "lexical": 0.6,
            "rrf": 0.8,
            "rerank": 0.9,
        }
        assert trace["generation"]["model"] == "fixture-model"
        assert trace["generation"]["prompt_tokens"] == 12
        assert trace["generation"]["completion_tokens"] == 8
        assert trace["generation"]["usage_status"] == "reported"
        assert len(trace["generation"]["prompt_template_sha256"]) == 64
        assert trace["retrieval"]["latency_ms"] >= 0
        assert trace["generation"]["latency_ms"] >= 0
        assert trace["verification"]["latency_ms"] >= 0
        assert trace["total_latency_ms"] >= trace["retrieval"]["latency_ms"]
        assert trace["rewrite"]["status"] == "unchanged"
        assert (
            trace["query"]["sha256"] == hashlib.sha256(f"Question for {ws}?".encode()).hexdigest()
        )
        assert "Private evidence" not in json.dumps(trace)
        assert "Question for" not in json.dumps(trace)
    # Per-call observers must never replace the shared engine's dependencies.
    assert engine.orchestrator is retrieval
    assert engine.llm is provider
    assert answers[0].debug_trace is not answers[1].debug_trace


@pytest.mark.asyncio
async def test_stream_delivers_trace_only_on_verified_event_and_discloses_missing_usage():
    engine = AnswerEngine(orchestrator=FixtureRetriever(), llm=FixtureProvider(), model="fixture")
    events = [
        event async for event in engine.stream(query="Q", acl_scope=ACLScope(workspace_ids=["A"]))
    ]
    assert events[-1][0] == "verified"
    assert all(isinstance(value, str) for kind, value in events if kind == "token")
    answer = events[-1][1]
    assert isinstance(answer, Answer)
    assert answer.debug_trace["context"]["chunk_ids"] == ["chunk-A"]
    assert answer.debug_trace["generation"]["prompt_tokens"] is None
    assert answer.debug_trace["generation"]["usage_status"] == "not_reported"
    assert "Private evidence" not in json.dumps(answer.debug_trace)


@pytest.mark.asyncio
async def test_closing_stream_closes_underlying_provider_without_another_token():
    provider = FixtureProvider()
    engine = AnswerEngine(orchestrator=FixtureRetriever(), llm=provider, model="fixture")
    stream = engine.stream(query="Q", acl_scope=ACLScope(workspace_ids=["A"]))
    first = await anext(stream)
    assert first[0] == "token"
    await stream.aclose()
    assert provider.closed


@pytest.mark.asyncio
async def test_public_scope_context_matches_existing_acl_contract():
    class PublicRetriever:
        async def retrieve(self, **kwargs):
            return [
                RetrievedItem(
                    chunk_id="chunk-A",
                    document_id="doc-public",
                    workspace_id="shared",
                    content="Private evidence for A.",
                    score=0.5,
                )
            ]

    engine = AnswerEngine(orchestrator=PublicRetriever(), llm=FixtureProvider(), model="fixture")
    answer = await engine.answer(query="Q", acl_scope=ACLScope(include_public=True))
    assert answer.debug_trace["context"]["chunk_ids"] == ["chunk-A"]
    assert answer.debug_trace["filters"]["include_public"] is True


@pytest.mark.asyncio
async def test_unavailable_retrieval_is_not_reported_as_successful_generation():
    from retrieval.orchestrator import RetrievalResult, RetrievalStatus, retrieval_outcome_ctx

    class UnavailableRetriever:
        async def retrieve(self, **kwargs):
            retrieval_outcome_ctx.set(RetrievalResult(items=[], status=RetrievalStatus.UNAVAILABLE))
            return []

    class UnusedProvider(FixtureProvider):
        async def generate(self, request):
            raise AssertionError("unavailable retrieval must not invoke generation")

    engine = AnswerEngine(
        orchestrator=UnavailableRetriever(), llm=UnusedProvider(), model="fixture"
    )
    answer = await engine.answer(query="Q", acl_scope=ACLScope(workspace_ids=["A"]))
    assert answer.status.value == "unavailable"
    assert answer.debug_trace["retrieval"]["status"] == "unavailable"
    assert answer.debug_trace["generation"]["status"] == "not_run"
