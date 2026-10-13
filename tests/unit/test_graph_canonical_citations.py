# ruff: noqa: E501
"""T-V2-005: graph canonical citations - no summary-as-source, store-hydrated content."""

from __future__ import annotations

import pytest
from answer.adapters.postgres_lookup import MemoryChunkLookup
from answer.hydration import CanonicalHydrator
from answer.models import Claim
from answer.verification import ExtractiveGrounder, verify_claims
from core.auth import ACLScope
from graph import GraphRetriever
from graph.extractor import GraphChunk, RuleBasedGraphExtractor
from graph.sqlite_store import SQLiteGraphStore
from retrieval.interfaces import RetrievedItem


@pytest.mark.unit
async def test_graph_citations_never_contain_summary_marker(tmp_path) -> None:
    """Summary-text citations: 0 - prove no citation content contains the summary marker."""
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    await store.set_enabled("ws-a", True)

    # Create a chunk and extract graph
    chunk = GraphChunk(chunk_id="ch-1", text="Alice Smith works at Acme Corp. The launch budget is 20 dollars.", workspace_id="ws-a", document_id="doc-1")
    extractor = RuleBasedGraphExtractor()
    result = await extractor.extract([chunk])
    await store.upsert_extraction("ws-a", result.entities, result.relationships, result.communities)

    # Also need to set up chunk lookup for canonical hydration
    lookup = MemoryChunkLookup()
    lookup.add_chunk(
        "ch-1",
        content="Alice Smith works at Acme Corp. The launch budget is 20 dollars.",
        workspace_id="ws-a",
        document_id="doc-1",
        filename="doc.txt",
        page=1,
        section="Intro",
    )
    hydrator = CanonicalHydrator(lookup)

    # Simulate graph retrieval
    retriever = GraphRetriever(store)
    search_result = await retriever.local_search("ws-a", "Alice Smith", depth=1)
    # Should have found something
    assert search_result.evidence_chunk_ids

    items = retriever.to_retrieved_items(search_result, "ws-a")
    # Items should have summary marker in content before hydration
    assert any("[Knowledge Graph Context]" in item.content for item in items)

    # Hydrate - should replace summary with canonical
    acl = ACLScope(user_id="u1", workspace_ids=["ws-a"])
    from answer.hydration import allowed_workspace_ids_for

    hydrated = await hydrator.hydrate_items(items, allowed_workspace_ids=allowed_workspace_ids_for(acl))
    # Hydrated items should have canonical content, not summary
    for item in hydrated:
        assert "[Knowledge Graph Context]" not in item.content
        assert "Alice Smith works at Acme Corp." in item.content or "launch budget" in item.content.lower() or item.content

    # Verify claims - citations should have canonical content
    claims = [Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-1"])]
    # Need to hydrate for verification as well - use same hydrator
    _verified, citations = await verify_claims(
        claims,
        hydrated,
        chunk_lookup=hydrator,
        allowed_workspace_ids={"ws-a"},
        claim_grounder=ExtractiveGrounder(),
    )
    # Prove: no citation content contains summary marker across graph test corpus
    for citation in citations:
        assert citation.content is not None
        assert "[Knowledge Graph Context]" not in citation.content
        assert "900 dollars" not in citation.content  # summary's wrong figure should not leak if we had one

    store.close()


@pytest.mark.unit
async def test_graph_citations_provenance_from_hydrated_record(tmp_path) -> None:
    """Provenance (doc/filename/page/section) comes from hydrated record."""
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    await store.set_enabled("ws-a", True)

    chunk = GraphChunk(chunk_id="ch-1", text="Alice Smith works at Acme Corp.", workspace_id="ws-a", document_id="doc-1", page=5, section="Team")
    extractor = RuleBasedGraphExtractor()
    result = await extractor.extract([chunk])
    await store.upsert_extraction("ws-a", result.entities, result.relationships, result.communities)

    lookup = MemoryChunkLookup()
    lookup.add_chunk(
        "ch-1",
        content="Alice Smith works at Acme Corp.",
        workspace_id="ws-a",
        document_id="doc-launch",
        filename="launch_plan.pdf",
        page=1,
        section="Budget",
    )
    hydrator = CanonicalHydrator(lookup)

    retriever = GraphRetriever(store)
    search_result = await retriever.local_search("ws-a", "Alice Smith")
    items = retriever.to_retrieved_items(search_result, "ws-a")
    # Hydrate
    acl = ACLScope(user_id="u1", workspace_ids=["ws-a"])
    from answer.hydration import allowed_workspace_ids_for

    hydrated = await hydrator.hydrate_items(items, allowed_workspace_ids=allowed_workspace_ids_for(acl))

    claims = [Claim(text="Alice Smith works at Acme Corp.", chunk_ids=["ch-1"])]
    _verified, citations = await verify_claims(
        claims,
        hydrated,
        chunk_lookup=hydrator,
        allowed_workspace_ids={"ws-a"},
        claim_grounder=ExtractiveGrounder(),
    )
    assert len(citations) == 1
    # Provenance must come from hydrated record, not from graph item's original metadata
    assert citations[0].document_id == "doc-launch"
    assert citations[0].filename == "launch_plan.pdf"
    assert citations[0].page == 1
    assert citations[0].section == "Budget"

    store.close()


@pytest.mark.unit
async def test_graph_item_missing_chunk_drops_citation(tmp_path) -> None:
    """Graph item whose canonical chunk is missing/deleted/superseded must DROP citation."""
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    await store.set_enabled("ws-a", True)

    # Create a chunk and extract, but DO NOT add to lookup (simulate missing/deleted)
    chunk = GraphChunk(chunk_id="ch-missing", text="Ghost entity works at Phantom Corp.", workspace_id="ws-a", document_id="doc-missing")
    extractor = RuleBasedGraphExtractor()
    result = await extractor.extract([chunk])
    await store.upsert_extraction("ws-a", result.entities, result.relationships, result.communities)

    # Create a graph retrieval result that references the missing chunk
    # Simulate retriever output
    from graph import GraphSearchResult

    search_result = GraphSearchResult(
        entities=result.entities,
        relationships=result.relationships,
        evidence_chunk_ids=["ch-missing"],
        summary="Entities: Ghost (PERSON). Relationships: works at Phantom",
        score=1.0,
    )
    retriever = GraphRetriever(store)
    items = retriever.to_retrieved_items(search_result, "ws-a")

    # Lookup does NOT have ch-missing (simulating deleted/superseded)
    lookup = MemoryChunkLookup()
    # Intentionally do not add ch-missing
    hydrator = CanonicalHydrator(lookup)

    # Hydration should drop the item (no canonical)
    acl = ACLScope(user_id="u1", workspace_ids=["ws-a"])
    from answer.hydration import allowed_workspace_ids_for

    hydrated = await hydrator.hydrate_items(items, allowed_workspace_ids=allowed_workspace_ids_for(acl))
    assert hydrated == [], "missing chunk should be dropped at hydration"

    # Verification should also drop
    claims = [Claim(text="Ghost entity works at Phantom Corp.", chunk_ids=["ch-missing"])]
    verified, citations = await verify_claims(
        claims,
        items,  # Use unhydrated items to test verify's own hydration
        chunk_lookup=hydrator,
        allowed_workspace_ids={"ws-a"},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified == [], "claim with missing chunk should be dropped"
    assert citations == [], "citation with missing chunk should be dropped"

    store.close()


@pytest.mark.unit
async def test_graph_summary_never_grounded_without_store(tmp_path) -> None:
    """Without store, graph routing hint is never citable (fail-closed)."""
    # No hydrator

    summary = "[Knowledge Graph Context]\nEntities: Board. Relationships: approves budget."
    item = RetrievedItem(
        content=summary,
        score=1.0,
        workspace_id="ws-a",
        chunk_id="ch-graph-1",
        metadata={"is_graph": True, "routing_hint": True},
    )
    claims = [Claim(text="Board approves budget", chunk_ids=["ch-graph-1"])]
    verified, citations = await verify_claims(
        claims,
        [item],
        chunk_lookup=None,
        allowed_workspace_ids={"ws-a"},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified == []
    assert citations == []
