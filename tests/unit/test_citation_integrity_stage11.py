"""Stage 11 — deterministic, server-side citation integrity validator.

Service-free, hermetic, offline: the validator runs entirely on in-memory
``CanonicalChunk`` records with ``chunk_lookup=None`` (the store/lookup seam is
only an *optional* evidence source). No LLM, no network, no DB, no embeddings.

Covers every typed rejection reason in :class:`RejectionReason` and the
disclosure-propagation contract, plus an explicit hermeticity guard that scans
the module's import graph for LLM / network client dependencies.
"""

from __future__ import annotations

import ast
import inspect

import pytest
from answer.citation_integrity import (
    CandidateCitation,
    CitationIntegrityError,
    CitationIntegrityReport,
    RejectedCitation,
    RejectionReason,
    validate_citation_integrity,
)
from answer.models import CanonicalChunk, Claim, DocumentStatus
from retrieval.interfaces import RetrievalMethod, RetrievedItem

pytestmark = pytest.mark.unit

WS = "ws-a"
OTHER_WS = "ws-foreign"

#: Canonical passage the tests ground claims against.
CANONICAL_CONTENT = "The team met to review expenses. The launch budget is 20 dollars."


def _span(haystack: str, needle: str) -> tuple[int, int]:
    """Compute exact original-offset span of ``needle`` in ``haystack`` (case-sensitive)."""
    start = haystack.index(needle)
    return (start, start + len(needle))


def _chunk(
    chunk_id: str = "ch-1",
    *,
    workspace_id: str = WS,
    page: int | None = 1,
    section: str | None = "Budget",
    content: str = CANONICAL_CONTENT,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    metadata: dict | None = None,
) -> CanonicalChunk:
    return CanonicalChunk(
        chunk_id=chunk_id,
        workspace_id=workspace_id,
        document_id="doc-1",
        filename="launch_plan.pdf",
        page=page,
        section=section,
        content=content,
        status=status,
        metadata=metadata or {},
    )


# --------------------------------------------------------------------------------------
# Positive
# --------------------------------------------------------------------------------------


async def test_valid_canonical_citations_with_matching_span_and_quotation_accepted() -> None:
    """A real chunk, in-scope, active doc, matching page/section/quotation/span is accepted."""
    quote = "The launch budget is 20 dollars."
    span = _span(CANONICAL_CONTENT, quote)
    canonical = {"ch-1": _chunk(content=CANONICAL_CONTENT)}

    report = await validate_citation_integrity(
        claims=[Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-1"])],
        citations=[
            CandidateCitation(
                chunk_id="ch-1",
                claim_text="The launch budget is 20 dollars.",
                page=1,
                section="Budget",
                span=span,
                quotation=quote,
            )
        ],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )

    assert report.valid is True
    assert report.rejected_citations == []
    assert report.unverified_claims == []
    accepted = report.accepted_citations[0]
    assert accepted.chunk_id == "ch-1"
    assert accepted.page == 1
    assert accepted.section == "Budget"
    assert accepted.filename == "launch_plan.pdf"
    assert accepted.document_id == "doc-1"
    assert accepted.workspace_id == WS
    assert accepted.content == CANONICAL_CONTENT
    # The verbatim quotation span is preserved on the accepted citation.
    assert accepted.span == span
    assert accepted.grounding_score is None  # Stage 11 is integrity, not grounding.


async def test_citation_with_no_provenance_accepted_on_existence() -> None:
    """A citation carrying only a chunk_id is accepted when it resolves; provenance
    is filled in from the canonical record by the validator."""
    canonical = {"ch-1": _chunk(page=3, section="Results")}

    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="a claim")],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )

    assert report.valid is True
    assert len(report.accepted_citations) == 1
    accepted = report.accepted_citations[0]
    assert accepted.page == 3
    assert accepted.section == "Results"


async def test_valid_citations_with_chunk_lookup_seam() -> None:
    """The validator also accepts canonical evidence via the ChunkLookup boundary."""
    from answer.adapters.postgres_lookup import MemoryChunkLookup

    lookup = MemoryChunkLookup()
    lookup.add_chunk(
        "ch-1",
        content=CANONICAL_CONTENT,
        workspace_id=WS,
        document_id="doc-1",
        filename="launch_plan.pdf",
        page=1,
        section="Budget",
    )
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="budget is 20")],
        chunk_lookup=lookup,
        allowed_workspace_ids={WS},
    )
    assert report.valid is True
    assert len(report.accepted_citations) == 1
    assert report.accepted_citations[0].filename == "launch_plan.pdf"


# --------------------------------------------------------------------------------------
# Negative: workspace mismatch (foreign chunk)
# --------------------------------------------------------------------------------------


async def test_rejects_foreign_chunk_id_workspace_mismatch() -> None:
    """A chunk whose workspace is outside the caller's scope is FOREIGN_CHUNK."""
    chunk = _chunk(chunk_id="ch-foreign", workspace_id=OTHER_WS)
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-foreign", claim_text="x")],
        canonical={"ch-foreign": chunk},
        allowed_workspace_ids={WS},
    )
    assert report.valid is False
    assert len(report.rejected_citations) == 1
    rejected = report.rejected_citations[0]
    assert rejected.reason is RejectionReason.FOREIGN_CHUNK
    assert rejected.citation.chunk_id == "ch-foreign"


async def test_rejects_foreign_chunk_id_via_memory_lookup_seam() -> None:
    """The ChunkLookup seam must also scope by workspace."""
    from answer.adapters.postgres_lookup import MemoryChunkLookup

    lookup = MemoryChunkLookup()
    lookup.add_chunk("ch-foreign", content=CANONICAL_CONTENT, workspace_id=OTHER_WS)
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-foreign", claim_text="x")],
        chunk_lookup=lookup,
        allowed_workspace_ids={WS},
    )
    assert report.valid is False
    assert report.rejected_citations[0].reason is RejectionReason.FOREIGN_CHUNK


async def test_public_scope_accepts_every_workspace() -> None:
    """``allowed_workspace_ids=None`` (public/existence scope) accepts all workspaces."""
    canonical = {"ch-foreign": _chunk(chunk_id="ch-foreign", workspace_id=OTHER_WS)}
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-foreign", claim_text="x")],
        canonical=canonical,
        allowed_workspace_ids=None,
    )
    assert report.valid is True
    assert len(report.accepted_citations) == 1


# --------------------------------------------------------------------------------------
# Negative: deleted / superseded document
# --------------------------------------------------------------------------------------


async def test_rejects_deleted_document_chunk() -> None:
    chunk = _chunk(chunk_id="ch-dead", status=DocumentStatus.DELETED)
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-dead", claim_text="x")],
        canonical={"ch-dead": chunk},
        allowed_workspace_ids={WS},
    )
    assert report.valid is False
    assert report.rejected_citations[0].reason is RejectionReason.DELETED_DOCUMENT
    assert "deleted" in report.rejected_citations[0].detail.lower()


async def test_rejects_superseded_document_version() -> None:
    chunk = _chunk(chunk_id="ch-old", status=DocumentStatus.SUPERSEDED)
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-old", claim_text="x")],
        canonical={"ch-old": chunk},
        allowed_workspace_ids={WS},
    )
    assert report.valid is False
    assert report.rejected_citations[0].reason is RejectionReason.SUPERSEDED_VERSION


# --------------------------------------------------------------------------------------
# Negative: fabricated reference
# --------------------------------------------------------------------------------------


async def test_rejects_chunk_id_absent_from_canonical_store() -> None:
    """A chunk_id the model invented but the store has is FABRICATED_REFERENCE."""
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-does-not-exist", claim_text="x")],
        canonical={"ch-1": _chunk()},
        allowed_workspace_ids={WS},
    )
    assert report.valid is False
    assert report.rejected_citations[0].reason is RejectionReason.FABRICATED_REFERENCE
    assert "not present in the canonical store" in report.rejected_citations[0].detail


async def test_graph_summary_routing_hint_without_canonical_is_fabricated_reference() -> None:
    """A graph/summary item is a routing hint only; citable iff it hydrates canonically."""
    graph_item = RetrievedItem(
        content="[Knowledge Graph Context] budget of 900 dollars",
        score=1.0,
        method=RetrievalMethod.FUSED,
        workspace_id=WS,
        chunk_id="ch-graph",
        metadata={"is_graph": True, "routing_hint": True},
    )
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-graph", claim_text="budget 900")],
        items=[graph_item],
        canonical={},
        allowed_workspace_ids={WS},
    )
    assert report.valid is False
    assert report.rejected_citations[0].reason is RejectionReason.FABRICATED_REFERENCE
    assert "routing hint" in report.rejected_citations[0].detail


async def test_graph_summary_with_canonical_evidence_ignores_hint_text() -> None:
    """When the hint's chunk_id hydrates canonically, the (wrong) hint text is ignored."""
    graph_item = RetrievedItem(
        content="[Knowledge Graph Context] the budget is NINE HUNDRED dollars",
        score=1.0,
        method=RetrievalMethod.FUSED,
        workspace_id=WS,
        chunk_id="ch-1",
        metadata={"is_graph": True, "routing_hint": True},
    )
    canonical = {"ch-1": _chunk(content=CANONICAL_CONTENT)}
    quote = "The launch budget is 20 dollars."
    report = await validate_citation_integrity(
        citations=[
            CandidateCitation(
                chunk_id="ch-1", claim_text="budget", page=1, quotation=quote
            )
        ],
        items=[graph_item],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert report.valid is True
    assert len(report.accepted_citations) == 1


# --------------------------------------------------------------------------------------
# Negative: fabricated page / section provenance
# --------------------------------------------------------------------------------------


async def test_rejects_fabricated_page_number_as_page_mismatch() -> None:
    canonical = {"ch-1": _chunk(page=1, section="Budget")}
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="x", page=5)],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert report.valid is False
    assert report.rejected_citations[0].reason is RejectionReason.PAGE_MISMATCH
    assert "page 5" in report.rejected_citations[0].detail


async def test_rejects_page_when_canonical_has_no_page() -> None:
    canonical = {"ch-1": _chunk(page=None)}
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="x", page=2)],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert report.rejected_citations[0].reason is RejectionReason.PAGE_MISMATCH
    assert "no canonical page" in report.rejected_citations[0].detail


async def test_rejects_fabricated_section_as_page_mismatch() -> None:
    canonical = {"ch-1": _chunk(page=1, section="Budget")}
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="x", section="Findings")],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert report.rejected_citations[0].reason is RejectionReason.PAGE_MISMATCH
    assert "section" in report.rejected_citations[0].detail


async def test_rejects_section_when_canonical_has_no_section() -> None:
    canonical = {"ch-1": _chunk(section=None)}
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="x", section="Abstract")],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert report.rejected_citations[0].reason is RejectionReason.PAGE_MISMATCH


# --------------------------------------------------------------------------------------
# Negative: quotation / span provenance
# --------------------------------------------------------------------------------------


async def test_rejects_quotation_that_deviates_from_canonical_content() -> None:
    quote = "The launch budget is 20 dollars."
    span = _span(CANONICAL_CONTENT, quote)
    bad_quotation = "The launch budget is 900 dollars."  # content deviates (900 vs 20)
    report = await validate_citation_integrity(
        citations=[
            CandidateCitation(
                chunk_id="ch-1", claim_text="budget", page=1, quotation=bad_quotation,
                span=span,
            )
        ],
        canonical={"ch-1": _chunk()},
        allowed_workspace_ids={WS},
    )
    assert report.valid is False
    assert report.rejected_citations[0].reason is RejectionReason.SPAN_MISMATCH
    assert bad_quotation in report.rejected_citations[0].detail
    assert quote in report.rejected_citations[0].detail


async def test_rejects_quotation_not_present_in_canonical_source() -> None:
    report = await validate_citation_integrity(
        citations=[
            CandidateCitation(
                chunk_id="ch-1", claim_text="budget", quotation="There were 1450 widgets sold"
            )
        ],
        canonical={"ch-1": _chunk()},
        allowed_workspace_ids={WS},
    )
    assert report.rejected_citations[0].reason is RejectionReason.SPAN_MISMATCH


async def test_rejects_span_pointing_outside_canonical_content() -> None:
    report = await validate_citation_integrity(
        citations=[
            CandidateCitation(
                chunk_id="ch-1", claim_text="budget", span=(0, 9999),
            )
        ],
        canonical={"ch-1": _chunk()},
        allowed_workspace_ids={WS},
    )
    assert report.rejected_citations[0].reason is RejectionReason.SPAN_MISMATCH
    assert "out of bounds" in report.rejected_citations[0].detail


async def test_quotation_casing_only_difference_is_accepted() -> None:
    """Provenance is faithful to text, not casing: a case-only difference still matches."""
    quote = "the launch budget is 20 dollars."  # differs only in casing
    report = await validate_citation_integrity(
        citations=[
            CandidateCitation(
                chunk_id="ch-1", claim_text="budget", page=1, quotation=quote
            )
        ],
        canonical={"ch-1": _chunk()},
        allowed_workspace_ids={WS},
    )
    assert report.valid is True
    assert len(report.accepted_citations) == 1


# --------------------------------------------------------------------------------------
# Disclosure propagation / OCR uncertainty
# --------------------------------------------------------------------------------------


_LOST_REGION = {
    "reason": "no_text_layer",
    "lost_pages": [2],
    "page_count": 2,
    "readable_pages": 1,
}


def _disclosure_metadata_nested() -> dict:
    """The canonical ``chunk.metadata["chunk_disclosure"]`` carrier form."""
    return {"chunk_disclosure": {"parse_partial": True, "parse_lost_regions": [_LOST_REGION]}}


async def test_unreadable_cited_page_is_rejected_and_disclosure_propagated() -> None:
    """Citing an unreadable page -> UNREADABLE_SOURCE; omissions carry the disclosure."""
    canonical = {"ch-1": _chunk(metadata=_disclosure_metadata_nested())}
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="x", page=2)],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert report.valid is False
    assert report.rejected_citations[0].reason is RejectionReason.UNREADABLE_SOURCE
    assert "page 2" in report.rejected_citations[0].detail
    assert any("[2]" in o for o in report.omissions)
    assert any("unavailable" in o for o in report.omissions)


async def test_disclosure_propagated_to_omitted_for_readable_cited_page() -> None:
    """A citation on a readable page is accepted, but the source disclosure is still
    surfaced as an omission so the answer is honest about the partial read."""
    canonical = {"ch-1": _chunk(page=1, metadata=_disclosure_metadata_nested())}
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="budget", page=1)],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert report.valid is True
    assert len(report.accepted_citations) == 1
    assert report.omissions, "disclosure must be propagated to the report"
    assert any("unavailable" in o for o in report.omissions)


async def test_top_level_disclosure_keys_are_normalized() -> None:
    """The ingest pipeline also mirrors disclosure as top-level metadata keys."""
    metadata = {
        "parse_partial": True,
        "extraction_report": {"ocr": True, "quality": "low"},
        "parse_lost_regions": [_LOST_REGION],
    }
    canonical = {"ch-1": _chunk(page=2, metadata=metadata)}
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="x", page=2)],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert report.rejected_citations[0].reason is RejectionReason.UNREADABLE_SOURCE
    assert any("extraction report" in o for o in report.omissions)
    assert any("parsed partially" in o for o in report.omissions)


async def test_extraction_report_disclosure_propagated_without_lost_pages() -> None:
    """OCR uncertainty without an unreadable page is propagated, not rejected."""
    metadata = {"extraction_report": {"ocr": True, "quality": "low"}}
    canonical = {"ch-1": _chunk(metadata=metadata)}
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="budget")],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert report.valid is True
    assert len(report.accepted_citations) == 1
    assert any("extraction report" in o for o in report.omissions)


# --------------------------------------------------------------------------------------
# Unverified claims
# --------------------------------------------------------------------------------------


async def test_claim_with_all_citations_rejected_is_unverified() -> None:
    """A claim whose every cited reference was rejected lands in unverified_claims."""
    canonical = {"ch-dead": _chunk(status=DocumentStatus.DELETED)}
    report = await validate_citation_integrity(
        claims=[Claim(text="The budget is 20 dollars.", chunk_ids=["ch-dead"])],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert "The budget is 20 dollars." in report.unverified_claims
    assert report.valid is False


async def test_claim_with_at_least_one_accepted_citation_is_verified() -> None:
    canonical = {
        "ch-dead": _chunk(chunk_id="ch-dead", status=DocumentStatus.DELETED),
        "ch-ok": _chunk(chunk_id="ch-ok"),
    }
    report = await validate_citation_integrity(
        claims=[Claim(text="budget", chunk_ids=["ch-dead", "ch-ok"])],
        canonical=canonical,
        allowed_workspace_ids={WS},
    )
    assert report.unverified_claims == []
    assert len(report.accepted_citations) == 1


# --------------------------------------------------------------------------------------
# Report shape
# --------------------------------------------------------------------------------------


async def test_report_attributes_are_typed_and_complete() -> None:
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="x")],
        canonical={"ch-1": _chunk()},
        allowed_workspace_ids={WS},
    )
    assert isinstance(report, CitationIntegrityReport)
    assert isinstance(report.valid, bool)
    assert isinstance(report.accepted_citations, list)
    assert isinstance(report.rejected_citations, list)
    assert isinstance(report.omissions, list)
    assert isinstance(report.unverified_claims, list)
    for rejected in report.rejected_citations:
        assert isinstance(rejected, RejectedCitation)
        assert isinstance(rejected.reason, RejectionReason)


# --------------------------------------------------------------------------------------
# Hermeticity: zero LLM / network dependencies
# --------------------------------------------------------------------------------------

#: Import roots that would pull an LLM provider or a network/store client onto the
#: validator's execution path. Importing the module must never load any of these.
_FORBIDDEN_MODULE_PREFIXES = {
    "litellm",
    "openai",
    "anthropic",
    "httpx",
    "requests",
    "urllib",
    "socket",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "redis",
    "boto3",
    "botocore",
    "neo4j",
    "qdrant",
    "chromadb",
}
#: LLM-abstraction symbols the validator must never reference.
_FORBIDDEN_ATTRS = {"LLMProvider", "GenerationRequest", "GenerationResult", "ChatMessage"}


def test_validate_citation_integrity_is_hermetic_offline() -> None:
    """The module must not import any LLM or network/store client dependency.

    Scans the module's *own* AST (imports + referenced names/attributes) so a
    mention of "LLM" in the docstring never false-triggers, and so transitive
    type-definition imports (e.g. ``retrieval.interfaces`` Protocols) are not
    flagged — only real client roots are.
    """
    import answer.citation_integrity as mod

    source = inspect.getsource(mod)
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
    violations = sorted(n for n in imported if n in _FORBIDDEN_MODULE_PREFIXES)
    assert not violations, f"Stage 11 imports a forbidden dependency: {violations}"

    forbidden_refs: list[str] = []
    for node in ast.walk(tree):
        name = _referenced_name(node)
        if name and name in _FORBIDDEN_ATTRS:
            forbidden_refs.append(name)
    assert not forbidden_refs, (
        f"Stage 11 references a forbidden LLM abstraction: {forbidden_refs}"
    )


def _referenced_name(node: ast.AST) -> str | None:
    """Return the simple name a node references (Import aliases / attribute / name)."""
    if isinstance(node, ast.Import):
        return next(iter(node.names)).name.split(".")[0]
    if isinstance(node, ast.ImportFrom):
        return node.module.split(".")[0] if node.module else ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


async def test_offline_run_with_no_lookup_and_no_items_succeeds() -> None:
    """With only an in-memory canonical mapping, the validator needs nothing else."""
    canonical = {"ch-1": _chunk()}
    report = await validate_citation_integrity(
        citations=[CandidateCitation(chunk_id="ch-1", claim_text="budget")],
        canonical=canonical,
        items=None,
        chunk_lookup=None,
        allowed_workspace_ids={WS},
    )
    assert report.valid is True
    assert len(report.accepted_citations) == 1


async def test_validator_is_deterministic_across_runs() -> None:
    """Same inputs produce identical reports (no entropy / ordering leaks)."""
    canonical = {
        "ch-dead": _chunk(chunk_id="ch-dead", status=DocumentStatus.DELETED),
        "ch-1": _chunk(),
    }
    citations = [
        CandidateCitation(chunk_id="ch-1", claim_text="budget", page=1),
        CandidateCitation(chunk_id="ch-dead", claim_text="ghost"),
        CandidateCitation(chunk_id="ch-missing", claim_text="nope"),
    ]
    first = await validate_citation_integrity(
        citations=citations, canonical=canonical, allowed_workspace_ids={WS}
    )
    second = await validate_citation_integrity(
        citations=citations, canonical=canonical, allowed_workspace_ids={WS}
    )
    assert first.model_dump() == second.model_dump()
    assert len(first.rejected_citations) == 2


def test_error_type_is_runtime_error_subtype() -> None:
    assert issubclass(CitationIntegrityError, RuntimeError)
