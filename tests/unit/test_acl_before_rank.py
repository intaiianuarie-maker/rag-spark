"""ACL filter applied BEFORE ranking — unit test with fake retrievers (R-004).

The discriminating scenario: workspace B holds the *highest-scoring* rows. If
an implementation ranks first and filters afterwards, workspace A gets nothing
(the top-k window is already full of foreign rows) and, worse, a leak passes
unchanged. The contract requires the filter to travel with the retriever call,
so this test asserts both the positive path and that counterfactual.
"""

from __future__ import annotations

import pytest
from core.auth import ACLScope, Principal, Role
from harness_fakes import (
    FakeLexicalStore,
    FakeVectorStore,
    ReferenceHybridOrchestrator,
    make_record,
)
from retrieval.interfaces import SearchFilter

pytestmark = pytest.mark.unit

COLLECTION = "acl"
DIMENSION = 8


def _ws_a_records() -> list:
    # Deliberately *weaker* vectors than ws-b: at top_k=2 the foreign rows win
    # both slots, so rank-then-filter loses every ws-a row.
    return [
        make_record(
            "a-1",
            "ws-a",
            "alpha evidence about permissions",
            vector=[0.9, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            page=1,
            section="Intro",
        ),
        make_record(
            "a-2",
            "ws-a",
            "alpha evidence about citations",
            vector=[0.8, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            page=2,
            section="Citations",
        ),
    ]


def _ws_b_records() -> list:
    # Identical vectors to a-1: they outrank (or tie with) every ws-a row.
    return [
        make_record(
            "b-1",
            "ws-b",
            "bravo secret phrase permissions",
            vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            document_id="doc-b",
            page=1,
            section="Secret",
        ),
        make_record(
            "b-2",
            "ws-b",
            "bravo secret phrase citations",
            vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            document_id="doc-b",
            page=2,
            section="Secret",
        ),
    ]


async def _store() -> FakeVectorStore:
    store = FakeVectorStore(dimension=DIMENSION)
    await store.ensure_collection(collection=COLLECTION, dimension=DIMENSION)
    await store.upsert(collection=COLLECTION, records=[*_ws_a_records(), *_ws_b_records()])
    return store


async def test_filter_arrives_with_the_retriever_call_not_afterwards() -> None:
    dense = await _store()
    lexical = FakeLexicalStore()
    await lexical.index(records=[*_ws_a_records(), *_ws_b_records()])
    scope = ACLScope(user_id="u1", workspace_ids=["ws-a"])

    orchestrator = ReferenceHybridOrchestrator(dense, lexical, collection=COLLECTION)
    items = await orchestrator.retrieve(
        query="alpha evidence permissions", acl_scope=scope, dense_top_k=2, fusion_top_k=4
    )

    # 1. The store was called with the workspace scope (filter applied at call time).
    assert dense.search_calls, "dense retriever was never called"
    assert dense.search_calls[0].workspace_ids == ["ws-a"]
    assert lexical.search_calls[0].workspace_ids == ["ws-a"]

    # 2. Nothing outside the scope survived.
    assert items, "in-scope evidence disappeared"
    assert {item.workspace_id for item in items} == {"ws-a"}

    # 3. The foreign workspace really does hold rows (the test cannot pass vacuously).
    foreign = await dense.search(
        collection=COLLECTION,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        top_k=10,
        query_filter=SearchFilter(workspace_ids=["ws-b"]),
    )
    assert {item.workspace_id for item in foreign} == {"ws-b"}


async def test_rank_then_filter_would_lose_everything_but_filter_then_rank_does_not() -> None:
    """Counterfactual: the exact failure mode ACL-before-rank exists to prevent."""
    dense = await _store()
    query_vector = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    # Naive path: rank both workspaces, keep top-2, filter afterwards -> nothing left.
    ranked_first = await dense.search(
        collection=COLLECTION,
        vector=query_vector,
        top_k=2,
        query_filter=SearchFilter(workspace_ids=["ws-a", "ws-b"]),
    )
    naive = [item for item in ranked_first if item.workspace_id == "ws-a"]
    assert ranked_first and not naive, (
        "scenario setup broken: ws-b rows must outrank ws-a rows at top_k=2"
    )

    # Contract path: the filter is applied by the store before ranking/truncation.
    filtered_first = await dense.search(
        collection=COLLECTION,
        vector=query_vector,
        top_k=2,
        query_filter=SearchFilter(workspace_ids=["ws-a"]),
    )
    assert filtered_first, "filter-before-rank must still return ws-a evidence"
    assert {item.workspace_id for item in filtered_first} == {"ws-a"}


async def test_empty_scope_never_reaches_the_retrievers() -> None:
    dense = await _store()
    lexical = FakeLexicalStore()
    await lexical.index(records=_ws_a_records())
    orchestrator = ReferenceHybridOrchestrator(dense, lexical, collection=COLLECTION)

    items = await orchestrator.retrieve(query="alpha evidence", acl_scope=ACLScope(user_id="u1"))
    assert items == []
    assert dense.search_calls == [], "an empty scope must not fan out to retrievers"


async def test_scope_predicates_and_principal_roles() -> None:
    scope = ACLScope(user_id="u1", workspace_ids=["ws-a"])
    assert scope.allows_workspace("ws-a")
    assert not scope.allows_workspace("ws-b")
    assert ACLScope(user_id="u1").is_empty, "no workspaces and no public flag = empty scope"
    assert not scope.is_empty

    public = ACLScope(user_id="u1", include_public=True)
    assert public.allows_workspace("ws-anything")

    principal = Principal(
        user_id="u1",
        is_authenticated=True,
        workspace_roles={"ws-a": Role.VIEWER, "ws-b": Role.MEMBER},
    )
    assert principal.can_read("ws-a") and principal.can_read("ws-b")
    assert not principal.can_write("ws-a"), "viewer must not have write access"
    assert principal.can_write("ws-b")
    assert not Principal(user_id="anon").can_read("ws-a"), "non-member must not read"
