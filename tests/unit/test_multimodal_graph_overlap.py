"""A chunk surfaced by BOTH graph and multimodal legs keeps its canonical label."""

from __future__ import annotations

import pytest
from core.auth import ACLScope
from retrieval.interfaces import RetrievalMode, RetrievedItem
from retrieval.multimodal_leg import MultimodalLeg
from retrieval.orchestrator import HybridRetrievalOrchestrator
from test_graph_leg_ask_wiring import CanonicalLexical, Embedder, Graph, Store
from test_multimodal_retrieval_leg import PAGE2, MMStore, Pages, _hit

pytestmark = pytest.mark.unit


async def _run(graph_refs):
    leg = MultimodalLeg(
        store=MMStore([_hit()]),
        embedder=Embedder(),
        resolve_pages=Pages(PAGE2),
        collection="knowledge_multimodal",
    )
    orch = HybridRetrievalOrchestrator(
        dense=Store(
            [
                RetrievedItem(
                    chunk_id="d0",
                    document_id="docD",
                    workspace_id="A",
                    content="dense text",
                    score=0.9,
                )
            ]
        ),
        embedding=Embedder(),
        lexical=CanonicalLexical({"c1": "A"}),
        collection="fx",
        graph=Graph(graph_refs),
        multimodal=leg,
    )
    result = await orch.retrieve_result(
        query="q", acl_scope=ACLScope(workspace_ids=["A"]), mode=RetrievalMode.HYBRID_GRAPH
    )
    return {i.chunk_id: i for i in result.items}


@pytest.mark.asyncio
async def test_graph_and_multimodal_overlap_is_not_labelled_visual_only() -> None:
    items = await _run(["c1"])
    assert {"graph", "multimodal"} <= set(items["c1"].scores)
    assert "extraction" not in items["c1"].metadata
    assert "visual_only" not in items["c1"].metadata
    assert items["c2"].metadata["visual_only"] is True  # multimodal-only stays labelled


@pytest.mark.asyncio
async def test_multimodal_only_chunk_is_still_visual_only() -> None:
    items = await _run([])
    assert items["c1"].metadata["extraction"] == "vision"
