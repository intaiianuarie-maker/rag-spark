"""Product orchestrator contract (TASK-M3-004).

The M0-004 hook `test_retrieval_orchestrator_contract.py` runs the *reference*
orchestrator; this file runs the **product** one (`HybridRetrievalOrchestrator`)
through the same shared checkers so the seam is satisfied by the landed code:

- `check_retrieval_results_contract` — bounded, in-scope, citation-ready output
- `check_rrf_determinism` — the product fusion function is pinned

Plus the fail-closed empty-scope ruling and the five-key trace presence rule.
"""

from __future__ import annotations

import pytest
from core.auth import ACLScope
from harness_fakes import (
    FakeEmbeddingProvider,
    FakeLexicalStore,
    FakeReranker,
    FakeVectorStore,
    make_record,
)
from retrieval.contract_tests import (
    check_retrieval_results_contract,
    check_rrf_determinism,
)
from retrieval.orchestrator import HybridRetrievalOrchestrator, reciprocal_rank_fusion

pytestmark = [pytest.mark.unit, pytest.mark.contract]

COLLECTION = "contract"
DIMENSION = 8


def _records() -> list:
    def vector(*head: float) -> list[float]:
        return [*head, *([0.0] * (DIMENSION - len(head)))]

    return [
        make_record(
            "a-1",
            "ws-a",
            "hybrid retrieval evidence about permissions",
            vector=vector(1.0),
            page=1,
            section="Introduction",
        ),
        make_record(
            "a-2",
            "ws-a",
            "hybrid retrieval ranks evidence before context selection",
            vector=vector(0.8, 0.2),
            page=2,
            section="Method",
        ),
        make_record(
            "b-1",
            "ws-b",
            "bravo secret phrase hybrid retrieval",
            vector=vector(1.0),
            document_id="doc-b",
            page=1,
            section="Secret",
        ),
    ]


async def _pipeline() -> tuple[HybridRetrievalOrchestrator, FakeVectorStore, FakeLexicalStore]:
    dense = FakeVectorStore(dimension=DIMENSION)
    lexical = FakeLexicalStore()
    await dense.ensure_collection(collection=COLLECTION, dimension=DIMENSION)
    records = _records()
    await dense.upsert(collection=COLLECTION, records=records)
    await lexical.index(records=records)
    orchestrator = HybridRetrievalOrchestrator(
        dense=dense,
        embedding=FakeEmbeddingProvider(),
        lexical=lexical,
        collection=COLLECTION,
        reranker=FakeReranker(),
    )
    return orchestrator, dense, lexical


async def test_product_orchestrator_satisfies_the_result_contract() -> None:
    orchestrator, _, _ = await _pipeline()
    items = await orchestrator.retrieve(
        query="hybrid retrieval evidence",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
        context_top_k=5,
    )
    assert items, "orchestrator returned nothing for its own workspace"
    check_retrieval_results_contract(items, acl_scope_workspaces=["ws-a"], top_k=5)
    assert all("rrf" in item.scores for item in items), "fusion score missing from trace"


async def test_product_rrf_is_pinned() -> None:
    check_rrf_determinism(reciprocal_rank_fusion)


async def test_product_orchestrator_empty_scope_fails_closed() -> None:
    orchestrator, dense, _ = await _pipeline()
    before = len(dense.search_calls)
    items = await orchestrator.retrieve(
        query="hybrid retrieval", acl_scope=ACLScope(user_id="u1")
    )
    assert items == []
    assert len(dense.search_calls) == before
