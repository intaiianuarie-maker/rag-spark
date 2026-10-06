"""RetrievalOrchestrator contract (`retrieval.interfaces.RetrievalOrchestrator`).

The product orchestrator does not exist yet (M3-001b); the harness runs a
labelled reference implementation over fake retrievers so the seam is locked
today: bounded output, citation-ready provenance, finite scores, deterministic
RRF and workspace-scope enforcement. M3-001b satisfies the *same* assertions by
running `check_retrieval_results_contract` against its own implementation.
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
from retrieval.contract_tests import check_retrieval_results_contract

pytestmark = [pytest.mark.unit, pytest.mark.contract]

COLLECTION = "contract"
DIMENSION = 8  # matches FakeEmbeddingProvider.dimension


def _records() -> list:
    return [
        make_record(
            "a-1",
            "ws-a",
            "hybrid retrieval evidence about permissions",
            vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            page=1,
            section="Introduction",
        ),
        make_record(
            "a-2",
            "ws-a",
            "hybrid retrieval ranks evidence before context selection",
            vector=[0.8, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            page=2,
            section="Method",
        ),
        make_record(
            "b-1",
            "ws-b",
            "bravo secret phrase hybrid retrieval",
            vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            document_id="doc-b",
            page=1,
            section="Secret",
        ),
    ]


async def _pipeline() -> tuple[ReferenceHybridOrchestrator, FakeVectorStore, FakeLexicalStore]:
    dense = FakeVectorStore(dimension=DIMENSION)
    lexical = FakeLexicalStore()
    await dense.ensure_collection(collection=COLLECTION, dimension=DIMENSION)
    records = _records()
    await dense.upsert(collection=COLLECTION, records=records)
    await lexical.index(records=records)
    orchestrator = ReferenceHybridOrchestrator(dense, lexical, collection=COLLECTION)
    return orchestrator, dense, lexical


async def test_reference_orchestrator_satisfies_the_result_contract() -> None:
    orchestrator, _, _ = await _pipeline()
    items = await orchestrator.retrieve(
        query="hybrid retrieval evidence",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
        fusion_top_k=5,
    )
    assert items, "orchestrator returned nothing for its own workspace"
    check_retrieval_results_contract(items, acl_scope_workspaces=["ws-a"], top_k=5)
    assert all("rrf" in item.scores for item in items), "fusion score missing from trace"


async def test_orchestrator_output_is_deterministic_across_calls() -> None:
    orchestrator, _, _ = await _pipeline()
    scope = ACLScope(user_id="u1", workspace_ids=["ws-a"])
    first = await orchestrator.retrieve(query="hybrid retrieval", acl_scope=scope)
    second = await orchestrator.retrieve(query="hybrid retrieval", acl_scope=scope)
    assert [item.model_dump() for item in first] == [item.model_dump() for item in second]


async def test_empty_acl_scope_returns_nothing() -> None:
    orchestrator, dense, _ = await _pipeline()
    before = len(dense.search_calls)
    items = await orchestrator.retrieve(query="hybrid retrieval", acl_scope=ACLScope(user_id="u1"))
    assert items == [], "empty scope must fail closed (R-004)"
    assert len(dense.search_calls) == before, "retrievers must not be called for an empty scope"


async def test_default_top_k_values_match_the_retrieval_spec() -> None:
    from retrieval.interfaces import (
        DEFAULT_CONTEXT_TOP_K,
        DEFAULT_DENSE_TOP_K,
        DEFAULT_FUSION_TOP_K,
        DEFAULT_LEXICAL_TOP_K,
        DEFAULT_RERANK_TOP_K,
    )

    assert (
        DEFAULT_DENSE_TOP_K,
        DEFAULT_LEXICAL_TOP_K,
        DEFAULT_FUSION_TOP_K,
        DEFAULT_RERANK_TOP_K,
        DEFAULT_CONTEXT_TOP_K,
    ) == (30, 30, 20, 10, 6)
