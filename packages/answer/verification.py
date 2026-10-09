"""Citation verification (TASK-M4-002).

A claim's ``chunk_id`` is valid only if it resolves to a chunk that **exists**,
is **inside the caller's workspace scope**, and **substantiates the claim**.
The first two are existence/scope checks; the third is **grounding** — the
claim's text must be supported by the chunk the model actually saw in context.
An optional :class:`~answer.interfaces.ChunkLookup` adds a store-backed
existence check for ids that were not in the retrieved top-k.

When a :class:`ClaimGrounder` is configured (the engine wires one by default),
a citation is added only when the cited chunk grounds the claim; citations to
chunks that do not support the claim are dropped, and a claim left with no
grounded citations is dropped too. Lookup-resolved citations (chunk not in the
retrieved context) cannot be grounded — there is no chunk content to score —
so they are dropped when a grounder is configured, keeping the verified set
strictly to chunks that were retrieved *and* support the claim.

**v1 interim strategy — conservative extractive grounding.** The default
:class:`ExtractiveGrounder` considers a claim grounded only when its text
appears **verbatim** (case-insensitive, contiguous substring) in the chunk.
This is deliberately strict: it rejects the support-vs-contradiction failures
that a token-overlap scorer cannot see (negation removal, entity/role swap,
quantity or date changes — these share most tokens with the contradicting
source), and it never grants HIGH confidence on paraphrase it cannot verify.
Because a translation's claim text is *not* a substring of a source in a
different language, cross-language answers are **not** accepted as grounded by
this interim mode; that is the separate multilingual release gate. A future
semantic (embedding) grounder can replace this behind the unchanged
:class:`ClaimGrounder` interface.

No citation is ever fabricated: an invalid id is dropped, and a claim left with
no valid ids is dropped from the output.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from retrieval.interfaces import RetrievedItem

from .interfaces import ChunkLookup
from .models import Citation, Claim


def _span(claim_text: str, chunk_text: str) -> tuple[int, int] | None:
    """Locate the claim text inside the chunk; ``None`` when not found."""
    index = chunk_text.lower().find(claim_text.lower())
    if index == -1:
        return None
    return (index, index + len(claim_text))


def _citation(
    chunk_id: str,
    item: RetrievedItem,
    claim_text: str,
    grounding_score: float | None = None,
) -> Citation:
    return Citation(
        chunk_id=chunk_id,
        document_id=item.document_id,
        filename=item.metadata.get("filename") if item.metadata else None,
        page=item.page,
        section=item.section,
        span=_span(claim_text, item.content),
        workspace_id=item.workspace_id,
        grounding_score=grounding_score,
    )


@runtime_checkable
class ClaimGrounder(Protocol):
    """Decides whether a claim is substantiated by a chunk's content.

    ``score`` returns a float in ``[0, 1]`` (``1`` = fully grounded). A citation
    is accepted only when ``score(claim_text, chunk_content) >= threshold``.

    Implementations are product glue: v1 default is :class:`ExtractiveGrounder`
    (verbatim-substring check); a future semantic (embedding) grounder can
    replace it behind this same interface without touching :func:`verify_claims`.
    """

    threshold: float

    def score(self, claim_text: str, chunk_content: str) -> float:
        """Return grounding score in ``[0, 1]`` for this claim/chunk pair."""
        ...


class ExtractiveGrounder:
    """Default v1 grounder (interim, conservative).

    A claim is grounded **only** when its text appears verbatim — case-insensitive
    and contiguous — inside the chunk's content. This is an intentional
    limitation of the v1 extractive mode: it cannot validate paraphrase or
    translation, so such claims are *not* grounded (they abstain at
    ``INSUFFICIENT_EVIDENCE`` rather than risk a false ``HIGH``).

    Token-overlap scoring is *not* used for the decision, because it cannot
    distinguish support from contradiction: negation removal, entity/role swap
    and quantity/date changes all share most tokens with the contradicting
    source (see ``v1-rereview.md`` P1). Verbatim containment rejects all of
    those. Cross-language claims are likewise rejected (a translation's text is
    not a substring of a source in another language); that is the separate
    multilingual release gate, not a v1 extractive limitation.
    """

    threshold: float

    def __init__(self, threshold: float = 1.0) -> None:
        self.threshold = threshold

    def score(self, claim_text: str, chunk_content: str) -> float:
        claim = claim_text.strip()
        if not claim:
            return 0.0
        return 1.0 if claim.casefold() in chunk_content.casefold() else 0.0


async def verify_claims(
    claims: Sequence[Claim],
    items: Sequence[RetrievedItem],
    *,
    chunk_lookup: ChunkLookup | None = None,
    allowed_workspace_ids: set[str] | None = None,
    claim_grounder: ClaimGrounder | None = None,
) -> tuple[list[Claim], list[Citation]]:
    """Return the claims that still have at least one valid, in-scope, *grounded*
    citation.

    A ``chunk_id`` is valid only when it resolves to a chunk that **exists**, is
    **inside the caller's workspace scope**, and (when ``claim_grounder`` is set)
    **substantiates the claim**: ``grounder.score(claim.text, chunk.content) >=
    grounder.threshold``. Citations that fail grounding are dropped, and a claim
    left with no surviving citation is dropped from the output.
    Lookup-resolved citations (chunk not in the retrieved context) cannot be
    grounded — there is no chunk content to score — so they are dropped when a
    grounder is configured, keeping the verified set strictly to chunks that were
    retrieved *and* support the claim.

    When ``claim_grounder`` is ``None`` the grounding gate is skipped and the
    behaviour is existence + scope only (the legacy contract).
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
            if item is not None:
                grounding_score: float | None = None
                if claim_grounder is not None:
                    grounding_score = claim_grounder.score(claim.text, item.content)
                    if grounding_score < claim_grounder.threshold:
                        # Chunk does not substantiate the claim — not a citation.
                        continue
                valid_ids.append(chunk_id)
                citations.append(_citation(chunk_id, item, claim.text, grounding_score))
            elif chunk_lookup is not None and claim_grounder is None:
                # Only existence/scope (no content to ground against).
                citation = await chunk_lookup.get(chunk_id)
                if citation is not None:
                    # Workspace invariant: lookup-resolved chunks must carry a
                    # workspace and must be inside the allowed scope (if set).
                    if citation.workspace_id is not None:
                        if (allowed_workspace_ids is None
                                or citation.workspace_id in allowed_workspace_ids):
                            valid_ids.append(chunk_id)
                            citations.append(citation)
        if valid_ids:
            verified.append(Claim(text=claim.text, chunk_ids=valid_ids))
    return verified, citations
