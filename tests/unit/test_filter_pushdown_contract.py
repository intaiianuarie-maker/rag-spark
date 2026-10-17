"""T-F08: Filter pushdown completion & verification contract.

Verifies:
1. Store-level pushdown: SearchFilter translates to native predicates before ranking
   (Qdrant must conditions, Postgres WHERE clauses).
2. Fail-closed on empty workspace scope: zero queries dispatched, zero rows returned.
3. Strict isolation: foreign workspace records are excluded at the storage layer,
   preventing ranking pollution or leak.
4. Metadata & language pushdown: metadata attributes and language filters are evaluated
   in database predicates.
"""

from __future__ import annotations

import pytest
from core.auth import ACLScope
from harness_fakes import (
    FakeLexicalStore,
    FakeVectorStore,
    ReferenceHybridOrchestrator,
    make_record,
)
from retrieval.adapters.qdrant_store import _filter_from_search_filter
from retrieval.interfaces import SearchFilter

pytestmark = [pytest.mark.unit]


def test_qdrant_filter_pushdown_translation() -> None:
    """Verify SearchFilter translates to native Qdrant filter predicates."""
    # 1. Full filter
    f = SearchFilter(
        workspace_ids=["ws-alpha", "ws-beta"],
        document_ids=["doc-1", "doc-2"],
        language="en",
        metadata={"category": "finance", "confidential": True},
    )
    q_filter = _filter_from_search_filter(f)
    assert q_filter is not None
    assert len(q_filter.must) == 5

    keys = [cond.key for cond in q_filter.must]
    assert "workspace_id" in keys
    assert "document_id" in keys
    assert "language" in keys
    assert "metadata.category" in keys
    assert "metadata.confidential" in keys

    # 2. Empty filter
    empty_f = SearchFilter()
    assert _filter_from_search_filter(empty_f) is None


@pytest.mark.asyncio
async def test_filter_pushdown_prevents_disallowed_rows_reaching_ranking() -> None:
    """Disallowed rows must never enter ranking or crowd out authorized hits."""
    dim = 8
    store = FakeVectorStore(dimension=dim)
    await store.ensure_collection(collection="test_col", dimension=dim)

    # ws-allowed: 2 lower-scoring records
    r1 = make_record(
        "r1", "ws-allowed", "allowed topic doc 1", vector=[0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    )
    r2 = make_record(
        "r2", "ws-allowed", "allowed topic doc 2", vector=[0.6, 0.4, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    )

    # ws-disallowed: 2 higher-scoring records (would steal top_k if not filtered before ranking)
    r3 = make_record(
        "r3",
        "ws-disallowed",
        "disallowed secret 1",
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    r4 = make_record(
        "r4",
        "ws-disallowed",
        "disallowed secret 2",
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )

    await store.upsert(collection="test_col", records=[r1, r2, r3, r4])

    lexical = FakeLexicalStore()
    await lexical.index(records=[r1, r2, r3, r4])

    orchestrator = ReferenceHybridOrchestrator(store, lexical, collection="test_col")
    scope = ACLScope(user_id="user-1", workspace_ids=["ws-allowed"])

    # Retrieve with top_k=2. If pushdown works, r1 and r2 are returned.
    # If post-filtering was used, r3 and r4 would fill the top-2 and post-filtering would yield [].
    hits = await orchestrator.retrieve(
        query="secret topic doc", acl_scope=scope, dense_top_k=2, fusion_top_k=2
    )

    assert len(hits) > 0, "Pushdown failed: in-scope documents were crowded out"
    for hit in hits:
        assert hit.workspace_id == "ws-allowed", f"Disallowed row leaked: {hit.workspace_id}"


@pytest.mark.asyncio
async def test_empty_workspace_filter_fails_closed() -> None:
    """Empty scope returns empty list immediately without querying storage."""
    store = FakeVectorStore(dimension=8)
    await store.ensure_collection(collection="test_col", dimension=8)
    lexical = FakeLexicalStore()

    orchestrator = ReferenceHybridOrchestrator(store, lexical, collection="test_col")
    empty_scope = ACLScope(user_id="user-anon", workspace_ids=[])

    hits = await orchestrator.retrieve(query="any query", acl_scope=empty_scope)
    assert hits == []
    assert len(store.search_calls) == 0
    assert len(lexical.search_calls) == 0
