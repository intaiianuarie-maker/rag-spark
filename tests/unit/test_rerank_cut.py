"""Unit tests for T-ARCH-03 rerank decision cut and dense-only disclosure.

Verifies:
1. Local cross-encoder reranker instantiation refusal (LocalRerankerRemovedError).
2. Factory defaults to reranker=None, rerank_enabled=False.
3. Orchestrator retrieve_result sets rerank_disclosure and attaches it to item metadata.
4. Fallback reranker failure sets rerank_disclosure.
5. Search route response carries rerank_disclosure.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from app.search.search import SearchRequest, search
from core.auth import ACLScope, Principal
from harness_fakes import (
    FakeEmbeddingProvider,
    FakeLexicalStore,
    FakeVectorStore,
    make_record,
)
from retrieval.adapters.infinity_rerank import (
    InfinityReranker,
    LocalRerankerRemovedError,
    RerankerError,
)
from retrieval.interfaces import Reranker, RetrievedItem
from retrieval.orchestrator import (
    DENSE_ONLY_DISCLOSURE,
    HybridRetrievalOrchestrator,
    RetrievalStatus,
)

pytestmark = pytest.mark.unit

COLLECTION = "test-rerank-cut"
DIMENSION = 8


def _records() -> list:
    def vector(*head: float) -> list[float]:
        return [*head, *([0.0] * (DIMENSION - len(head)))]

    return [
        make_record(
            "doc-1",
            "ws-1",
            "first evidence text about reranking cut and dense retrieval",
            vector=vector(1.0),
            page=1,
            section="Section 1",
        ),
        make_record(
            "doc-2",
            "ws-1",
            "second evidence text verifying disclosure behavior",
            vector=vector(0.8, 0.2),
            page=2,
            section="Section 2",
        ),
    ]


async def _orchestrator(*, reranker: Reranker | None = None, rerank_enabled: bool = False):
    dense = FakeVectorStore(dimension=DIMENSION)
    lexical = FakeLexicalStore()
    await dense.ensure_collection(collection=COLLECTION, dimension=DIMENSION)
    records = _records()
    await dense.upsert(collection=COLLECTION, records=records)
    await lexical.index(records=records)
    return HybridRetrievalOrchestrator(
        dense=dense,
        embedding=FakeEmbeddingProvider(),
        lexical=lexical,
        collection=COLLECTION,
        reranker=reranker,
        rerank_enabled=rerank_enabled,
    )


def test_local_reranker_construction_refused() -> None:
    """Instantiating InfinityReranker with default/local URL raises LocalRerankerRemovedError."""
    with pytest.raises(LocalRerankerRemovedError) as exc_default:
        InfinityReranker()
    assert "permanently removed" in str(exc_default.value)

    with pytest.raises(LocalRerankerRemovedError) as exc_local:
        InfinityReranker(base_url="http://localhost:7997")
    assert "permanently removed" in str(exc_local.value)

    with pytest.raises(LocalRerankerRemovedError) as exc_ip:
        InfinityReranker(base_url="http://127.0.0.1:7997")
    assert "permanently removed" in str(exc_ip.value)


def test_explicit_mock_url_permitted_for_contracts() -> None:
    """Test harnesses and contract tests can pass an explicit non-local mock URL."""
    reranker = InfinityReranker(base_url="http://mock-service.local:8080")
    assert reranker.base_url == "http://mock-service.local:8080"


def test_factory_creates_orchestrator_without_reranker(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_orchestrator() sets reranker=None and rerank_enabled=False."""
    monkeypatch.setenv("VECTOR_COLLECTION", COLLECTION)
    from answer.factory import create_orchestrator

    orch = create_orchestrator(collection=COLLECTION)
    assert orch.reranker is None
    assert orch.rerank_enabled is False


async def test_dense_only_retrieval_discloses_rerank_deletion() -> None:
    """When reranking is disabled / absent, result and items disclose dense-only status."""
    orch = await _orchestrator(reranker=None, rerank_enabled=False)
    acl_scope = ACLScope(user_id="user-1", workspace_ids=["ws-1"])

    result = await orch.retrieve_result(query="evidence text", acl_scope=acl_scope)
    assert result.status == RetrievalStatus.OK
    assert result.rerank_disclosure == DENSE_ONLY_DISCLOSURE
    assert len(result.items) > 0
    for item in result.items:
        assert item.metadata.get("rerank_disclosure") == DENSE_ONLY_DISCLOSURE


class FailingReranker(Reranker):
    async def rerank(
        self, *, query: str, documents: Sequence[RetrievedItem], top_k: int | None = None
    ) -> list[RetrievedItem]:
        raise RerankerError("simulated connection failure")


async def test_reranker_fallback_discloses_dense_only() -> None:
    """When a configured reranker fails, fallback to RRF also sets rerank_disclosure."""
    orch = await _orchestrator(reranker=FailingReranker(), rerank_enabled=True)
    acl_scope = ACLScope(user_id="user-1", workspace_ids=["ws-1"])

    result = await orch.retrieve_result(query="evidence text", acl_scope=acl_scope)
    assert result.status == RetrievalStatus.DEGRADED
    assert result.rerank_disclosure == DENSE_ONLY_DISCLOSURE
    for item in result.items:
        assert item.metadata.get("rerank_disclosure") == DENSE_ONLY_DISCLOSURE


async def test_search_endpoint_returns_rerank_disclosure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search API route returns rerank_disclosure on response and items."""
    orch = await _orchestrator(reranker=None, rerank_enabled=False)

    monkeypatch.setattr("app.search.search.create_orchestrator", lambda: orch)

    ws_id = uuid.uuid4()
    # Mock require authorization check
    monkeypatch.setattr("app.search.search.require", lambda *args, **kwargs: None)

    principal = Principal(user_id="test-user")
    request = SearchRequest(query="evidence text")

    response = await search(workspace_id=ws_id, body=request, principal=principal)
    assert response.rerank_disclosure == DENSE_ONLY_DISCLOSURE
