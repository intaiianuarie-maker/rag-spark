"""Stage 11 — server-side deterministic citation integrity validator.

A citation integrity validator that runs on Contabo (server-side) and uses
**no LLM and no remote network calls** — every check is a pure, deterministic
comparison against *canonical hydrated evidence* (the authoritative chunk record
the store resolved through its document version, ACL scope and original text).

It is deliberately distinct from :mod:`answer.verification` (which performs
*claim grounding* — does the chunk's text support the claim). Stage 11 answers a
narrower, honesty-first question first:

    * Does the cited chunk actually exist in the canonical store?
    * Is it inside the caller's authorized workspace?
    * Is its source document active (not tombstoned / superseded)?
    * Does the asserted page & section match the chunk's canonical provenance
      (no fabricated pages or figures)?
    * Does an asserted quotation match the canonical source text at the
      original offsets?
    * Is the cited region readable, or does the chunk's parse/OCR disclosure
      say part of the source was never read (and that part is what is cited)?
    * Is the citation backed by canonical evidence at all — a synthesized graph
      summary is a routing hint only, never primary evidence?

The only backing dependency involved is the in-process :class:`ChunkLookup`
(e.g. :class:`~answer.adapters.postgres_lookup.PostgresChunkLookup`), which the
validator treats as an in-process callable — it never opens its own sockets, and
the hermetic test path passes an in-memory ``CanonicalChunk`` mapping with
``chunk_lookup=None`` so the pass is provably offline.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from retrieval.interfaces import RetrievedItem

from .interfaces import ChunkLookup
from .models import CanonicalChunk, Citation, Claim, DocumentStatus

logger = logging.getLogger("knowledge_os.answer.citation_integrity")

__all__ = [
    "CandidateCitation",
    "CitationIntegrityError",
    "CitationIntegrityReport",
    "RejectedCitation",
    "RejectionReason",
    "validate_citation_integrity",
]


class RejectionReason(StrEnum):
    """Typed rejection reasons exported on :class:`RejectedCitation`.

    The members mirror the contract in T-OR-05: every reason a citation can fail
    is named explicitly so callers never pattern-match on free-form strings.
    """

    #: The chunk_id does not exist in the canonical store. This covers
    #: model-fabricated ids and ids whose only "evidence" was a graph/summary
    #: routing hint (summaries are hints, never primary evidence).
    FABRICATED_REFERENCE = "FABRICATED_REFERENCE"
    #: The chunk exists canonically but lives in a workspace the caller is not
    #: authorized to read.
    FOREIGN_CHUNK = "FOREIGN_CHUNK"
    #: The chunk's document was tombstoned (``documents.deleted_at`` set).
    DELETED_DOCUMENT = "DELETED_DOCUMENT"
    #: The chunk's document version was retired (``superseded_at`` set).
    SUPERSEDED_VERSION = "SUPERSEDED_VERSION"
    #: Asserted page or section provenance disagrees with the canonical chunk.
    PAGE_MISMATCH = "PAGE_MISMATCH"
    #: An asserted quotation or span does not match canonical source text.
    SPAN_MISMATCH = "SPAN_MISMATCH"
    #: The cited region carries parse/OCR disclosure saying it was never read.
    UNREADABLE_SOURCE = "UNREADABLE_SOURCE"


class CandidateCitation(BaseModel):
    """One citation as asserted by the candidate answer / model.

    Carries both the chunk id and the *provenance the model claims* (page,
    section, span, quotation) so the validator can detect fabricated provenance
    independently of canonical existence. When citations are derived from a
    :class:`~answer.models.Claim` (which only carries ``chunk_ids``), the
    provenance fields are left ``None`` and only existence/scope/lifecycle checks
    run.
    """

    chunk_id: str
    claim_text: str = ""
    page: int | None = None
    section: str | None = None
    span: tuple[int, int] | None = None
    quotation: str | None = None


class RejectedCitation(BaseModel):
    """A candidate citation that failed an integrity check, with the typed cause."""

    citation: CandidateCitation
    reason: RejectionReason
    detail: str = ""


class CitationIntegrityReport(BaseModel):
    """Typed result of a deterministic citation-integrity pass (Stage 11).

    * ``valid`` — no citation was rejected and no claim was left unverified.
    * ``accepted_citations`` — citations that survived every check, rebuilt as
      :class:`~answer.models.Citation` records with canonical provenance.
    * ``rejected_citations`` — citations that failed, each carrying a typed
      :class:`RejectionReason`.
    * ``omissions`` — human-readable disclosure strings (unreadable pages, OCR
      uncertainty) propagated from source limitations to affected citations.
    * ``unverified_claims`` — claim texts whose *every* cited reference was
      rejected (text only; semantic grounding is :mod:`answer.verification`'s job).
    """

    valid: bool
    accepted_citations: list[Citation] = Field(default_factory=list)
    rejected_citations: list[RejectedCitation] = Field(default_factory=list)
    omissions: list[str] = Field(default_factory=list)
    unverified_claims: list[str] = Field(default_factory=list)


class CitationIntegrityError(RuntimeError):
    """Raised when the integrity pass cannot run deterministically (never on citation failure)."""


# --------------------------------------------------------------------------------------
# Disclosure / OCR helpers (pure, hermetic)
# --------------------------------------------------------------------------------------


def _coerce_int(value: Any) -> int | None:
    """Best-effort int coercion of a page number coming from untyped metadata."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _resolve_disclosure(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a chunk's parse/OCR disclosure into a uniform shape.

    Ingestion (:func:`apps.worker.jobs.ingest_pipeline.parse_disclosure`) writes
    the disclosure under the nested ``chunk_disclosure`` key and also mirrors
    ``parse_lost_regions`` / ``extraction_report`` / ``parse_partial`` onto the
    chunk's top-level metadata. This reader accepts both carriers so the validator
    is robust to whichever seam hydrated the record.
    """
    if not metadata:
        return {
            "lost_pages": [],
            "regions": [],
            "extraction_report": None,
            "parse_partial": False,
        }
    base: dict[str, Any] = dict(metadata)
    carrier = metadata.get("chunk_disclosure")
    if isinstance(carrier, dict) and carrier:
        # Nested carrier is authoritative; top-level keys are the mirror.
        for key, value in carrier.items():
            base.setdefault(key, value)

    lost_regions_raw = base.get("parse_lost_regions")
    regions: list[dict[str, Any]] = []
    lost_pages: list[int] = []
    if isinstance(lost_regions_raw, list):
        for region in lost_regions_raw:
            if isinstance(region, dict):
                regions.append(region)
                for page in region.get("lost_pages", []) or []:
                    page_int = _coerce_int(page)
                    if page_int is not None:
                        lost_pages.append(page_int)
    # Fallback carrier: a bare ``lost_pages`` list on the disclosure.
    bare_pages = base.get("lost_pages")
    if isinstance(bare_pages, list):
        for page in bare_pages:
            page_int = _coerce_int(page)
            if page_int is not None and page_int not in lost_pages:
                lost_pages.append(page_int)
    return {
        "lost_pages": sorted(set(lost_pages)),
        "regions": regions,
        "extraction_report": base.get("extraction_report"),
        "parse_partial": bool(base.get("parse_partial")),
    }


def _disclosure_omission(chunk_id: str, disclosure: Mapping[str, Any]) -> str:
    """Render a human-readable, front-end-safe disclosure string for a chunk.

    Emits one entry per lost region (with its reason) when region detail is
    available, falling back to a flat unreadable-pages summary otherwise; never
    double-reports the same region.
    """
    parts: list[str] = []
    regions = disclosure.get("regions") or []
    lost_pages = disclosure.get("lost_pages") or []
    if regions:
        for region in regions:
            if not isinstance(region, dict):
                continue
            reason = region.get("reason", "unreadable")
            region_pages = region.get("lost_pages")
            if isinstance(region_pages, list) and region_pages:
                pages = ", ".join(str(p) for p in region_pages)
                parts.append(
                    f"chunk {chunk_id}: pages [{pages}] carry {reason}; "
                    "that content is unavailable"
                )
            else:
                parts.append(f"chunk {chunk_id}: unreadable region ({reason})")
    elif lost_pages:
        pages = ", ".join(str(p) for p in lost_pages)
        parts.append(
            f"chunk {chunk_id}: pages [{pages}] unreadable (no text layer); "
            "that content is unavailable"
        )
    if disclosure.get("extraction_report") is not None:
        parts.append(
            f"chunk {chunk_id}: OCR/extraction report present (text quality may be reduced)"
        )
    if disclosure.get("parse_partial"):
        parts.append(f"chunk {chunk_id}: source parsed partially (some regions missing)")
    return " | ".join(parts)


def _is_routing_hint(item: RetrievedItem) -> bool:
    """True when the retrieved item's own text is a *routing hint*, never evidence."""
    metadata = item.metadata or {}
    return bool(metadata.get("is_graph") or metadata.get("routing_hint"))


def _is_in_workspace(chunk: CanonicalChunk, allowed: set[str] | None) -> bool:
    """Mirror :func:`answer.hydration.allowed_workspace_ids_for` scope semantics."""
    if allowed is None:
        return True  # public / existence-only scope: accept every workspace.
    if chunk.workspace_id is None:
        return False  # unscoped chunk is not visible to a workspace-scoped caller.
    return chunk.workspace_id in allowed


def _locate_span(needle: str, haystack: str) -> tuple[int, int] | None:
    """Case-insensitive location of ``needle`` in ``haystack`` returning original offsets."""
    if not needle:
        return None
    match = re.search(re.escape(needle), haystack, re.IGNORECASE)
    if match is None:
        return None
    return (match.start(), match.end())


def _equal_text(left: str, right: str) -> bool:
    """Exact equality, tolerant of casing-only differences (provenance-faithful)."""
    if left == right:
        return True
    return left.casefold() == right.casefold()


def _claim_had_citations(claim: Claim, candidates: Sequence[CandidateCitation]) -> bool:
    return any(c.claim_text == claim.text for c in candidates)


# --------------------------------------------------------------------------------------
# Provenance checks (each returns ok, detail, resolved_span)
# --------------------------------------------------------------------------------------


def _check_page(
    citation: CandidateCitation, chunk: CanonicalChunk
) -> tuple[bool, str]:
    if citation.page is None:
        return True, ""
    if chunk.page is None:
        return (
            False,
            f"citation asserts page {citation.page} but chunk {citation.chunk_id} "
            "has no canonical page provenance",
        )
    if citation.page != chunk.page:
        return (
            False,
            f"citation page {citation.page} != canonical page {chunk.page} "
            f"for chunk {citation.chunk_id}",
        )
    return True, ""


def _check_section(
    citation: CandidateCitation, chunk: CanonicalChunk
) -> tuple[bool, str]:
    if citation.section is None:
        return True, ""
    if chunk.section is None:
        return (
            False,
            f"citation asserts section {citation.section!r} but chunk "
            f"{citation.chunk_id} has no canonical section provenance",
        )
    if citation.section != chunk.section:
        return (
            False,
            f"citation section {citation.section!r} != canonical section "
            f"{chunk.section!r} for chunk {citation.chunk_id}",
        )
    return True, ""


def _check_quotation(
    citation: CandidateCitation, chunk: CanonicalChunk
) -> tuple[bool, str, tuple[int, int] | None]:
    """Verify an asserted quotation / span against canonical source text."""
    content = chunk.content
    resolved_span: tuple[int, int] | None = citation.span

    # 1. Span bounds must reference real offsets inside the canonical content.
    if citation.span is not None:
        start, end = citation.span
        if content is None:
            return (
                False,
                f"span {citation.span} asserted for chunk {citation.chunk_id} "
                "but canonical content is empty",
                None,
            )
        if start < 0 or end > len(content) or start >= end:
            return (
                False,
                f"span {citation.span} out of bounds for chunk {citation.chunk_id} "
                f"(content length {len(content)})",
                None,
            )

    # 2. If a quotation was asserted, it MUST match the canonical source text.
    if citation.quotation is not None:
        if content is None:
            return (
                False,
                f"quotation asserted for chunk {citation.chunk_id} but "
                "canonical content is empty",
                None,
            )
        if citation.span is not None:
            start, end = citation.span
            source_slice = content[start:end]
            resolved_span = (start, end)
        else:
            located = _locate_span(citation.quotation, content)
            if located is None:
                return (
                    False,
                    f"quotation {citation.quotation!r} not found in canonical "
                    f"source of chunk {citation.chunk_id}",
                    None,
                )
            source_slice = content[located[0] : located[1]]
            resolved_span = located
        if not _equal_text(source_slice, citation.quotation):
            return (
                False,
                f"quotation {citation.quotation!r} does not match canonical source "
                f"{source_slice!r} for chunk {citation.chunk_id}",
                None,
            )

    # 3. Best-effort provenance span for the accepted citation.
    if resolved_span is None and citation.claim_text and content:
        located = _locate_span(citation.claim_text, content)
        if located is not None:
            resolved_span = located
    return True, "", resolved_span


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------


async def validate_citation_integrity(
    *,
    claims: Sequence[Claim] | None = None,
    citations: Sequence[CandidateCitation] | None = None,
    canonical: Mapping[str, CanonicalChunk] | None = None,
    items: Sequence[RetrievedItem] | None = None,
    chunk_lookup: ChunkLookup | None = None,
    allowed_workspace_ids: set[str] | None = None,
) -> CitationIntegrityReport:
    """Deterministically validate candidate citations against canonical evidence.

    Args:
        claims: candidate claims (each carries ``chunk_ids``). Used to populate
            ``unverified_claims`` when every citation a claim made was rejected.
        citations: explicit candidate citations carrying the *asserted* provenance
            (page/section/span/quotation) plus the claim they back. When omitted,
            one citation per claim ``chunk_id`` is derived (provenance unchecked).
        canonical: authoritative, store-hydrated chunk records keyed by chunk id.
            Carries lifecycle ``status`` and parse/OCR ``metadata`` disclosure, so
            this mapping is the only source that can yield the typed
            ``DELETED_DOCUMENT`` / ``SUPERSEDED_VERSION`` / ``UNREADABLE_SOURCE``
            reasons.
        items: retrieved items handed to the model (may include graph/summary
            routing hints). Used only to recognize hint-only evidence for a
            non-canonical chunk id; never used as grounding text.
        chunk_lookup: optional canonical lookup (e.g.
            :class:`~answer.adapters.postgres_lookup.PostgresChunkLookup`) used to
            fill in chunk ids not present in ``canonical``. A lookup-resolved
            record is always treated as ``ACTIVE`` (the store already filters
            deleted/superseded to ``None``), so typed lifecycle rejections come
            only from ``canonical``.
        allowed_workspace_ids: caller's authorized workspace scope (``None`` =
            public / existence-only, accepts every workspace).

    Returns a :class:`CitationIntegrityReport`. No LLM and no network are
    touched: with ``canonical`` supplied and ``chunk_lookup=None`` the pass is
    a pure in-memory comparison (the hermetic test path).
    """
    claims = list(claims or [])
    candidate_citations = list(citations or [])
    if not candidate_citations:
        candidate_citations = [
            CandidateCitation(chunk_id=chunk_id, claim_text=claim.text)
            for claim in claims
            for chunk_id in claim.chunk_ids
        ]

    items = list(items or [])
    items_by_chunk: dict[str, RetrievedItem] = {}
    hint_chunk_ids: set[str] = set()
    for retrieved_item in items:
        if not retrieved_item.chunk_id:
            continue
        if retrieved_item.chunk_id not in items_by_chunk:
            items_by_chunk[retrieved_item.chunk_id] = retrieved_item
        if _is_routing_hint(retrieved_item):
            hint_chunk_ids.add(retrieved_item.chunk_id)

    # Build the canonical registry. The explicit ``canonical`` mapping is
    # authoritative (status + disclosure aware); the lookup only fills gaps and
    # always yields ACTIVE records (the store filters lifecycle to None itself).
    registry: dict[str, CanonicalChunk] = {}
    if canonical:
        registry.update(canonical)
    referenced_ids = [c.chunk_id for c in candidate_citations]
    missing_ids = [cid for cid in dict.fromkeys(referenced_ids) if cid not in registry]
    if missing_ids and chunk_lookup is not None:
        resolved = await chunk_lookup.get_many(missing_ids)
        for chunk_id, lookup_citation in resolved.items():
            retrieved = items_by_chunk.get(chunk_id)
            item_meta = (
                dict(retrieved.metadata)
                if retrieved is not None and retrieved.metadata
                else {}
            )
            registry[chunk_id] = CanonicalChunk(
                chunk_id=chunk_id,
                workspace_id=lookup_citation.workspace_id,
                document_id=lookup_citation.document_id,
                document_version_id=None,
                filename=lookup_citation.filename,
                page=lookup_citation.page,
                section=lookup_citation.section,
                content=lookup_citation.content,
                status=DocumentStatus.ACTIVE,
                metadata=item_meta,
            )

    accepted: list[Citation] = []
    rejected: list[RejectedCitation] = []
    omissions: list[str] = []
    accepted_chunk_ids: set[str] = set()

    for candidate in candidate_citations:
        chunk = registry.get(candidate.chunk_id)

        # 1. Existence: must resolve to a canonical record. A graph/summary
        #    routing hint is NOT evidence — hint-only ids are fabricated refs.
        if chunk is None:
            if candidate.chunk_id in hint_chunk_ids:
                rejected.append(
                    RejectedCitation(
                        citation=candidate,
                        reason=RejectionReason.FABRICATED_REFERENCE,
                        detail=(
                            f"chunk id {candidate.chunk_id!r} is referenced only by a "
                            "graph/summary routing hint; summaries are hints, not "
                            "canonical evidence"
                        ),
                    )
                )
            else:
                rejected.append(
                    RejectedCitation(
                        citation=candidate,
                        reason=RejectionReason.FABRICATED_REFERENCE,
                        detail=(
                            f"chunk id {candidate.chunk_id!r} not present in the "
                            "canonical store (fabricated reference)"
                        ),
                    )
                )
            continue

        # 2. Document lifecycle (tombstone / superseded).
        if chunk.status is DocumentStatus.DELETED:
            rejected.append(
                RejectedCitation(
                    citation=candidate,
                    reason=RejectionReason.DELETED_DOCUMENT,
                    detail=(
                        f"chunk {candidate.chunk_id} belongs to a deleted (tombstoned) "
                        f"document {chunk.document_id or '<unknown>'}"
                    ),
                )
            )
            continue
        if chunk.status is DocumentStatus.SUPERSEDED:
            rejected.append(
                RejectedCitation(
                    citation=candidate,
                    reason=RejectionReason.SUPERSEDED_VERSION,
                    detail=(
                        f"chunk {candidate.chunk_id} belongs to a superseded document "
                        f"version {chunk.document_version_id or '<unknown>'}"
                    ),
                )
            )
            continue

        # 3. Workspace access.
        if not _is_in_workspace(chunk, allowed_workspace_ids):
            rejected.append(
                RejectedCitation(
                    citation=candidate,
                    reason=RejectionReason.FOREIGN_CHUNK,
                    detail=(
                        f"chunk {candidate.chunk_id} workspace {chunk.workspace_id!r} "
                        "not in the caller's authorized scope"
                    ),
                )
            )
            continue

        # 4. Parse/OCR disclosure: reject citations pointing at an unreadable
        #    region, and propagate the disclosure as an omission otherwise.
        disclosure = _resolve_disclosure(chunk.metadata)
        lost_pages = disclosure["lost_pages"]
        if candidate.page is not None and int(candidate.page) in lost_pages:
            omitted = _disclosure_omission(candidate.chunk_id, disclosure)
            if omitted:
                omissions.append(omitted)
            rejected.append(
                RejectedCitation(
                    citation=candidate,
                    reason=RejectionReason.UNREADABLE_SOURCE,
                    detail=(
                        f"page {candidate.page} of chunk {candidate.chunk_id} is "
                        "unreadable (no text layer) per the source disclosure"
                    ),
                )
            )
            continue

        # 5. Page & section provenance (no fabricated pages/figures).
        page_ok, page_detail = _check_page(candidate, chunk)
        if not page_ok:
            rejected.append(
                RejectedCitation(
                    citation=candidate, reason=RejectionReason.PAGE_MISMATCH, detail=page_detail
                )
            )
            continue
        section_ok, section_detail = _check_section(candidate, chunk)
        if not section_ok:
            rejected.append(
                RejectedCitation(
                    citation=candidate, reason=RejectionReason.PAGE_MISMATCH, detail=section_detail
                )
            )
            continue

        # 6. Exact quotation / span provenance.
        quote_ok, quote_detail, resolved_span = _check_quotation(candidate, chunk)
        if not quote_ok:
            rejected.append(
                RejectedCitation(
                    citation=candidate, reason=RejectionReason.SPAN_MISMATCH, detail=quote_detail
                )
            )
            continue

        # 7. Accepted — propagate disclosure for otherwise-valid citations, then
        #    rebuild as a verified Citation with canonical provenance.
        omitted = _disclosure_omission(candidate.chunk_id, disclosure)
        if omitted:
            omissions.append(omitted)
        accepted.append(
            Citation(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                filename=chunk.filename,
                page=chunk.page,
                section=chunk.section,
                span=resolved_span,
                workspace_id=chunk.workspace_id,
                grounding_score=None,
                content=chunk.content,
            )
        )
        accepted_chunk_ids.add(candidate.chunk_id)

    # Claims left with no surviving citation (every referenced id was rejected).
    accepted_texts = {
        c.claim_text
        for c in candidate_citations
        if c.claim_text and c.chunk_id in accepted_chunk_ids
    }
    unverified_claims: list[str] = []
    for claim in claims:
        if _claim_had_citations(claim, candidate_citations) and claim.text not in accepted_texts:
            unverified_claims.append(claim.text)

    report = CitationIntegrityReport(
        valid=not rejected and not unverified_claims,
        accepted_citations=accepted,
        rejected_citations=rejected,
        omissions=omissions,
        unverified_claims=unverified_claims,
    )
    if rejected:
        logger.info(
            "citation_integrity_rejections",
            extra={
                "event": "citation_integrity_rejections",
                "counts": {
                    reason.value: sum(1 for r in rejected if r.reason == reason)
                    for reason in RejectionReason
                },
                "accepted": len(accepted),
            },
        )
    return report
