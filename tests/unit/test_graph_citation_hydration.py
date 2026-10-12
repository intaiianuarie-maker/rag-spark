"""Unit and regression tests for graph citation hydration (RP-03 / QF-03).

Verifies the 20-vs-900 budget control:
- Claims can NEVER be grounded against synthesized [Knowledge Graph Context] text.
- Evidence chunk IDs from graph retrieval MUST be authoritatively hydrated.
- Hallucinated or mutated claims (e.g. 900 vs 20) are strictly rejected.
- Deleted, superseded, or foreign-workspace chunks are dropped.
"""

import pytest
from answer.adapters.postgres_lookup import MemoryChunkLookup
from answer.models import Claim
from answer.verification import ExtractiveGrounder, verify_claims
from retrieval.interfaces import RetrievalMethod, RetrievedItem


def _make_graph_item(
    chunk_id: str,
    *,
    summary: str = "Entities: Acme, Alice. The launch budget is 900 dollars.",
    workspace_id: str = "ws-prod",
) -> RetrievedItem:
    return RetrievedItem(
        content=f"[Knowledge Graph Context]\n{summary}",
        score=1.0,
        method=RetrievalMethod.FUSED,
        workspace_id=workspace_id,
        chunk_id=chunk_id,
        metadata={
            "is_graph": True,
            "entities_count": 2,
            "relationships_count": 1,
        },
    )


@pytest.mark.unit
async def test_graph_citation_synthetic_summary_without_lookup_rejected() -> None:
    """Without authoritative chunk hydration, synthesized graph items cannot cite chunks."""
    item = _make_graph_item("ch-100", summary="The launch budget is 900 dollars.")
    claim = Claim(text="The launch budget is 900 dollars.", chunk_ids=["ch-100"])

    verified, citations = await verify_claims(
        claims=[claim],
        items=[item],
        chunk_lookup=None,  # No authoritative lookup
        allowed_workspace_ids={"ws-prod"},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified == []
    assert citations == []


@pytest.mark.unit
async def test_graph_citation_hydration_rejects_hallucinated_claim_20_vs_900() -> None:
    """The 20-vs-900 control: synthesized 900 budget is rejected against original 20 budget."""
    item = _make_graph_item("ch-100", summary="The launch budget is 900 dollars.")
    lookup = MemoryChunkLookup()
    lookup.add_chunk(
        "ch-100",
        content="The team met to review expenses. The launch budget is 20 dollars.",
        workspace_id="ws-prod",
        document_id="doc-launch",
        page=1,
        section="Budget",
    )

    # Claim claiming 900 dollars (from hallucinated/synthesized graph relation)
    bad_claim = Claim(text="The launch budget is 900 dollars.", chunk_ids=["ch-100"])

    verified, citations = await verify_claims(
        claims=[bad_claim],
        items=[item],
        chunk_lookup=lookup,
        allowed_workspace_ids={"ws-prod"},
        claim_grounder=ExtractiveGrounder(),
    )
    # 900 does NOT appear in the original passage (budget is 20), so it must be rejected!
    assert verified == []
    assert citations == []


@pytest.mark.unit
async def test_graph_citation_hydration_accepts_authentic_claim() -> None:
    """Authentic claim matching the hydrated original passage is accepted with real provenance."""
    item = _make_graph_item("ch-100", summary="The launch budget is 900 dollars.")
    lookup = MemoryChunkLookup()
    lookup.add_chunk(
        "ch-100",
        content="The team met to review expenses. The launch budget is 20 dollars.",
        workspace_id="ws-prod",
        document_id="doc-launch",
        filename="launch_plan.pdf",
        page=1,
        section="Budget",
    )

    good_claim = Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-100"])

    verified, citations = await verify_claims(
        claims=[good_claim],
        items=[item],
        chunk_lookup=lookup,
        allowed_workspace_ids={"ws-prod"},
        claim_grounder=ExtractiveGrounder(),
    )
    assert len(verified) == 1
    assert verified[0].chunk_ids == ["ch-100"]
    assert len(citations) == 1
    assert citations[0].document_id == "doc-launch"
    assert citations[0].filename == "launch_plan.pdf"
    assert citations[0].page == 1
    assert citations[0].section == "Budget"
    assert citations[0].span is not None
    assert citations[0].grounding_score == 1.0


@pytest.mark.unit
async def test_graph_citation_hydration_rejects_deleted_or_superseded_chunk() -> None:
    """Deleted or superseded chunks must never be cited."""
    item = _make_graph_item("ch-del")
    lookup = MemoryChunkLookup()
    lookup.add_chunk(
        "ch-del",
        content="The launch budget is 20 dollars.",
        workspace_id="ws-prod",
        is_deleted=True,
    )

    claim = Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-del"])
    verified, citations = await verify_claims(
        claims=[claim],
        items=[item],
        chunk_lookup=lookup,
        allowed_workspace_ids={"ws-prod"},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified == []
    assert citations == []


@pytest.mark.unit
async def test_graph_citation_hydration_rejects_foreign_workspace() -> None:
    """Cross-workspace chunks must never be cited even if matching."""
    item = _make_graph_item("ch-foreign", workspace_id="ws-prod")
    lookup = MemoryChunkLookup()
    lookup.add_chunk(
        "ch-foreign",
        content="The launch budget is 20 dollars.",
        workspace_id="ws-foreign",  # Different workspace in store
    )

    claim = Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-foreign"])
    verified, citations = await verify_claims(
        claims=[claim],
        items=[item],
        chunk_lookup=lookup,
        allowed_workspace_ids={"ws-prod"},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified == []
    assert citations == []
