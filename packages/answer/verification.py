"""Citation verification (TASK-M4-002).

A claim's ``chunk_id`` is valid only if it resolves to a chunk that **exists**
and is **inside the caller's workspace scope**. The default source of truth is
the retrieved item set (already ACL-filtered by the orchestrator), so a
``chunk_id`` the model invented or that belongs to another workspace is stripped.
An optional :class:`~answer.interfaces.ChunkLookup` adds a store-backed
existence check for ids that were not in the retrieved top-k.

No citation is ever fabricated: an invalid id is dropped, and a claim left with
no valid ids is dropped from the output.
"""

from __future__ import annotations

from collections.abc import Sequence

from retrieval.interfaces import RetrievedItem

from .interfaces import ChunkLookup
from .models import Citation, Claim


def _span(claim_text: str, chunk_text: str) -> tuple[int, int] | None:
    """Locate the claim text inside the chunk; ``None`` when not found."""
    index = chunk_text.lower().find(claim_text.lower())
    if index == -1:
        return None
    return (index, index + len(claim_text))


def _citation(chunk_id: str, item: RetrievedItem, claim_text: str) -> Citation:
    return Citation(
        chunk_id=chunk_id,
        document_id=item.document_id,
        filename=item.metadata.get("filename") if item.metadata else None,
        page=item.page,
        section=item.section,
        span=_span(claim_text, item.content),
        workspace_id=item.workspace_id,
    )


async def verify_claims(
    claims: Sequence[Claim],
    items: Sequence[RetrievedItem],
    *,
    chunk_lookup: ChunkLookup | None = None,
    allowed_workspace_ids: set[str] | None = None,
) -> tuple[list[Claim], list[Citation]]:
    """Return the claims that still have at least one valid, in-scope chunk id.

    A ``chunk_id`` is valid only when it resolves to a chunk that **exists** and
    is **inside the caller's workspace scope**. When ``allowed_workspace_ids`` is
    given, retrieved items outside it (or with no workspace) are ignored, so a
    store/orchestrator that leaked a foreign row cannot be cited. Each surviving
    claim keeps only its valid ids; citations are built from the matching
    retrieved items (provenance + span).
    """
    by_chunk = {
        item.chunk_id: item
        for item in items
        if item.chunk_id
        and item.workspace_id is not None
        and (allowed_workspace_ids is None or item.workspace_id in allowed_workspace_ids)
    }

    verified: list[Claim] = []
    citations: list[Citation] = []
    for claim in claims:
        valid_ids: list[str] = []
        for chunk_id in claim.chunk_ids:
            item = by_chunk.get(chunk_id)
            if item is None and chunk_lookup is not None:
                citation = await chunk_lookup.get(chunk_id)
                if citation is not None:
                    # Workspace invariant: lookup-resolved chunks must carry a
                    # workspace and must be inside the allowed scope (if set).
                    if citation.workspace_id is not None:
                        if (allowed_workspace_ids is None
                                or citation.workspace_id in allowed_workspace_ids):
                            valid_ids.append(chunk_id)
                            citations.append(citation)
            if item is not None:
                valid_ids.append(chunk_id)
                citations.append(_citation(chunk_id, item, claim.text))
        if valid_ids:
            verified.append(Claim(text=claim.text, chunk_ids=valid_ids))
    return verified, citations
