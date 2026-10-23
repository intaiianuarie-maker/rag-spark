"""Graph extraction seam (V2 work package G1).

Turns provenance-carrying chunks into :class:`Entity` / :class:`Relationship`
nodes that a :class:`~graph.interfaces.GraphStore` can persist and that
:class:`~graph.retriever.GraphRetriever` can traverse.

Two implementations ship here:

* :class:`RuleBasedGraphExtractor` — deterministic regex NER + relation-cue
  extraction. Pure logic (no model weights, no API keys) so it is unit-testable
  and usable offline-first. It is the *ground truth* the LLM adapter is held
  against.
* The LLM adapter lives in :mod:`graph.adapters.lightrag_extractor` and reuses
  :class:`~models.interfaces.LLMProvider` (LiteLLM) behind LightRAG's
  extract-entities/relations prompt pattern.

Contract notes (V2-GRAPH-PLAN seam, Section 2):

* Extraction never raises *per chunk*: malformed text / LLM parse failures are
  recorded as :class:`ExtractionError` on the result, mirroring the Q0
  ``reason_code`` convention without forking the answer taxonomy (Stream A owns
  Q0).
* Every produced node carries ``chunk_ids`` / ``document_ids`` provenance so the
  citation verifier can resolve a graph claim back to a stored chunk.
* ``workspace_id`` is stamped on every node at the source — ACL scoping starts
  here, not at retrieval time (plan rule 13).
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field

from .interfaces import (
    Entity,
    EntityType,
    GraphCommunity,
    Relationship,
    RelationshipType,
)
from .resolver import SimpleEntityResolver


@dataclass(frozen=True)
class GraphChunk:
    """A provenance-attributed chunk handed to a :class:`GraphExtractor`.

    Unlike the v1 ingestion ``Chunk`` (which carries a uuid row id and vector),
    this carries the *logical* identifiers the citation layer needs: the chunk
    id (as stored in the vector/lexical store), the owning workspace, and the
    source document/page/section for attribution.
    """

    chunk_id: str
    text: str
    workspace_id: str
    document_id: str | None = None
    page: int | None = None
    section: str | None = None
    language: str = "unknown"


@dataclass
class ExtractionResult:
    """Batch extraction output: entities, relationships, communities, provenance."""

    entities: list[Entity] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    communities: list[GraphCommunity] = field(default_factory=list)
    chunk_ids: list[str] = field(default_factory=list)
    failed_chunk_ids: list[str] = field(default_factory=list)
    errors: list[ExtractionError] = field(default_factory=list)


class ExtractionError(Exception):
    """A recoverable, reason-coded extraction failure (non-fatal per chunk).

    ``reason_code`` follows the shared Q0 contract (UPPER_SNAKE_CASE string) so
    extraction failures coexist with answer-layer failures (``PARSE_FALLBACK``
    / ``EVIDENCE_MISSING`` / ``CITATION_INVALID`` / ``PARTIAL_EVIDENCE``) without
    forking the taxonomy. ``workspace_id`` and ``chunk_id`` always identify the
    failed provenance so a skipped chunk is never silently counted as
    successful provenance.
    """

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        workspace_id: str | None = None,
        chunk_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.workspace_id = workspace_id
        self.chunk_id = chunk_id


class GraphExtractor(ABC):
    """Strategy interface for extracting a graph from text chunks."""

    @abstractmethod
    async def extract(self, chunks: Sequence[GraphChunk]) -> ExtractionResult:
        """Extract entities/relationships for the given chunks.

        Implementations MUST be chunk-scoped and workspace-scoped and MUST NOT
        raise on a single bad chunk — record an :class:`ExtractionError` instead.
        """
        raise NotImplementedError


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------

_TITLE = r"(?:Dr\.|Prof\.|Mr\.|Ms\.|Mrs\.)"
_FIRST_LAST = r"[A-Z][a-z]+ [A-Z][a-z]+"  # "Alice Smith"
_NAME = r"[A-Z][A-Za-z&'\-]+(?: [A-Z][A-Za-z&'\-]*)*"  # a capitalized phrase
_CORP = r"Corporation|Corp\.|Incorporated|Inc\.|LLC|Ltd\.|GmbH|SA|PLC|Limited"
_CORP_RE = re.compile(_CORP, re.IGNORECASE)
_TITLE_NAME_RE = re.compile(rf"\b(?:{_TITLE}\s+)?({_FIRST_LAST})\b")
_DATE_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def _node_id(prefix: str, workspace_id: str, digest: str) -> str:
    """Deterministic, workspace-scoped node id (stable across re-extracts)."""
    return f"{prefix}-{hashlib.sha256(f'{workspace_id}|{digest}'.encode()).hexdigest()[:10]}"


def _norm(name: str) -> str:
    """Normalize a surface name for entity matching.

    Stays in lock-step with ``resolver._normalize_name`` so an entity surfaced
    only inside a relation capture resolves to the same canonical id as the
    standalone mention of that entity.
    """
    s = name.lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for suffix in (
        " corporation", " corp", " incorporated", " inc", " llc", " ltd",
        " gmbh", " limited", " plc", " sa",
    ):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s


def _snippet(text: str, term: str, limit: int) -> str:
    """Return a sentence-bounded window around the first ``term`` occurrence."""
    if not term:
        return text[:limit].rstrip()
    start = text.find(term)
    if start == -1:
        return text[:limit].rstrip()
    for stop in (".", "!", "?", "\n"):
        prev = text.rfind(stop, 0, start)
        if prev != -1:
            start = prev + 1
            break
    end = len(text)
    for stop in (".", "!", "?"):
        nxt = text.find(stop, start)
        if nxt != -1:
            end = min(end, nxt + 1)
    window = text[start:end].strip()
    return (window[:limit] + "\u2026") if len(window) > limit else window


def _classify(name: str, hint: str | None = None) -> EntityType:
    """Infer an entity type from surface text and relation-role hint."""
    if hint == "location":
        return EntityType.LOCATION
    if hint == "product":
        return EntityType.PRODUCT
    if hint == "person":
        return EntityType.PERSON
    if hint == "organization":
        return EntityType.ORGANIZATION
    if _CORP_RE.search(name):
        return EntityType.ORGANIZATION
    if re.fullmatch(_FIRST_LAST, name):
        return EntityType.PERSON
    return EntityType.CONCEPT


def _reject_mixed_workspace(
    chunks: Sequence[GraphChunk],
) -> ExtractionError | None:
    """Return an ExtractionError when a batch spans more than one workspace.

    Extraction is chunk-scoped *and* workspace-scoped: a single ``extract``
    call must never mix workspaces, otherwise identical names in different
    workspaces could cross-resolve (R-004). Callers surface the returned error
    on the :class:`ExtractionResult` and emit no provenance for the batch.
    """
    workspaces = {c.workspace_id for c in chunks}
    if len(workspaces) > 1:
        return ExtractionError(
            "WORKSPACE_MISMATCH",
            "extract() refuses a mixed-workspace batch; workspaces: "
            + ", ".join(sorted(workspaces)),
        )
    return None


def _name_index(entities: Sequence[Entity]) -> dict[tuple[str, str], str]:
    """Map (workspace_id, normalized name/alias) -> canonical entity id.

    Scoped by workspace so identical names in different workspaces never
    resolve to each other's id (R-004 partitioning).
    """
    index: dict[tuple[str, str], str] = {}
    for ent in entities:
        key = (ent.workspace_id, _norm(ent.name))
        index.setdefault(key, ent.id)
        for alias in ent.aliases:
            index.setdefault((ent.workspace_id, _norm(alias)), ent.id)
    return index


def _dedupe_entities(entities: Sequence[Entity]) -> list[Entity]:
    """Best-effort dedupe by (normalized name, type) for the non-resolve path."""
    seen: set[tuple[str, str, str]] = set()
    out: list[Entity] = []
    for ent in entities:
        key = (ent.workspace_id, _norm(ent.name), str(ent.type).upper())
        if key in seen:
            continue
        seen.add(key)
        out.append(ent)
    return out


# --------------------------------------------------------------------------------------
# Relation-cue patterns
# --------------------------------------------------------------------------------------

#: (regex, relationship_type, source_hint, target_hint)
_REL_PATTERNS: list[tuple[str, RelationshipType, str | None, str | None]] = [
    (rf"({_FIRST_LAST})\s+(?:works at|works for|is the CEO of|is CEO of|employed by|joined)\s+([A-Z][A-Za-z&'\.]+)", RelationshipType.WORKS_FOR, "person", "organization"),  # noqa: E501
    (rf"({_NAME})\s+(?:is part of|belongs to|merged into|acquired by|subsidiary of)\s+({_NAME})", RelationshipType.PART_OF, "organization", "organization"),  # noqa: E501
    (rf"({_NAME})\s+(?:owns|acquired|signed)\s+({_NAME})", RelationshipType.OWNS, "organization", "organization"),  # noqa: E501
    (rf"({_NAME})\s+(?:is located in|based in|headquartered in|located in)\s+([A-Z][A-Za-z &'\-]+)", RelationshipType.LOCATED_IN, "organization", "location"),  # noqa: E501
    (rf"({_NAME})\s+(?:develops|produces|launched|released)\s+([A-Z][A-Za-z0-9 &'\-]+)", RelationshipType.PRODUCES, "organization", "product"),  # noqa: E501
    (rf"({_NAME})\s+(?:related to|connected to|partnered with|collaborates with)\s+({_NAME})", RelationshipType.RELATED_TO, None, None),  # noqa: E501
]

_REL_COMPILED: list[tuple[re.Pattern[str], RelationshipType, str | None, str | None]] = [
    (re.compile(pat), rtype, shint, thint) for pat, rtype, shint, thint in _REL_PATTERNS
]

#: Maps a relationship type to the entity role hints for its endpoints. Endpoints
#: not covered by a hint fall back to :func:`_classify` name inference.
_REL_HINTS: dict[RelationshipType, tuple[str | None, str | None]] = {
    RelationshipType.WORKS_FOR: ("person", "organization"),
    RelationshipType.PART_OF: ("organization", "organization"),
    RelationshipType.OWNS: ("organization", "organization"),
    RelationshipType.LOCATED_IN: ("organization", "location"),
    RelationshipType.PRODUCES: ("organization", "product"),
    RelationshipType.RELATED_TO: (None, None),
    RelationshipType.MENTIONS: (None, None),
    RelationshipType.DEPENDS_ON: ("organization", "organization"),
    RelationshipType.SIGNED: ("organization", "organization"),
}


@dataclass(slots=True)
class _RelTriple:
    """A name-keyed relationship awaiting resolution to entity ids."""

    source_name: str
    target_name: str
    relationship_type: RelationshipType
    description: str
    chunk: GraphChunk


def _materialize_endpoint_entities(
    triples: Sequence[_RelTriple],
    max_description: int,
) -> list[Entity]:
    """Build Entity nodes for every relation endpoint (type via role hint)."""
    found: list[Entity] = []
    seen: set[tuple[str, str, str]] = set()
    for triple in triples:
        chunk = triple.chunk
        shint, thint = _REL_HINTS.get(triple.relationship_type, (None, None))
        for name, hint in ((triple.source_name, shint), (triple.target_name, thint)):
            etype = _classify(name, hint)
            key = (chunk.workspace_id, _norm(name), str(etype).upper())
            if key in seen:
                continue
            seen.add(key)
            found.append(
                Entity(
                    id=_node_id("ent", chunk.workspace_id, f"{etype}|{_norm(name)}"),
                    name=name,
                    type=etype,
                    description=_snippet(chunk.text, name, max_description),
                    workspace_id=chunk.workspace_id,
                    chunk_ids=[chunk.chunk_id],
                    document_ids=[chunk.document_id] if chunk.document_id else [],
                    properties={
                        "page": chunk.page,
                        "section": chunk.section,
                        "language": chunk.language,
                    },
                )
            )
    return found


def _build_relationships(
    triples: Sequence[_RelTriple],
    name_index: dict[tuple[str, str], str],
) -> list[Relationship]:
    """Resolve name-keyed triples to id-keyed edges, merging shared provenance."""
    out: list[Relationship] = []
    merged: dict[tuple[str, str, str], Relationship] = {}
    for triple in triples:
        chunk = triple.chunk
        ws = chunk.workspace_id
        source_id = name_index.get((ws, _norm(triple.source_name)))
        target_id = name_index.get((ws, _norm(triple.target_name)))
        if not source_id or not target_id:
            continue
        key = (source_id, target_id, str(triple.relationship_type))
        rel = merged.get(key)
        if rel is None:
            rel = Relationship(
                id=_node_id("rel", chunk.workspace_id, f"{key[0]}|{key[1]}|{key[2]}"),
                source_entity_id=source_id,
                target_entity_id=target_id,
                type=triple.relationship_type,
                description=triple.description,
                weight=1.0,
                workspace_id=chunk.workspace_id,
                chunk_ids=[chunk.chunk_id],
                document_ids=[chunk.document_id] if chunk.document_id else [],
                properties={"mention_count": 1},
            )
            merged[key] = rel
            out.append(rel)
        else:
            if chunk.chunk_id not in rel.chunk_ids:
                rel.chunk_ids.append(chunk.chunk_id)
            if chunk.document_id and chunk.document_id not in rel.document_ids:
                rel.document_ids.append(chunk.document_id)
            rel.properties["mention_count"] = int(rel.properties.get("mention_count", 1)) + 1
    return out


class RuleBasedGraphExtractor(GraphExtractor):
    """Deterministic, dependency-free graph extractor.

    Scans each chunk for relation cue phrases (works at, located in, owns, is
    part of, produces, related to) and emits typed :class:`Relationship` nodes
    along with the :class:`Entity` nodes they reference. Standalone title-prefixed
    person mentions, corporate-suffixed organizations, and four-digit years are
    also extracted.

    Entities mentioned only inside relation captures are materialized from the
    captures so every edge has resolvable endpoints. Results are passed through
    :class:`SimpleEntityResolver` to merge duplicate mentions and aggregate
    chunk provenance.

    The patterns above are the spec — tests assert exact extraction output.
    """

    def __init__(
        self,
        *,
        resolve: bool = True,
        max_description: int = 200,
        manual_aliases: dict[str, str] | None = None,
    ) -> None:
        self._resolve = resolve
        self._max_description = max_description
        self._resolver = SimpleEntityResolver(manual_aliases=manual_aliases)

    async def extract(
        self, chunks: Sequence[GraphChunk]
    ) -> ExtractionResult:
        if not chunks:
            return ExtractionResult()
        violation = _reject_mixed_workspace(chunks)
        if violation is not None:
            return ExtractionResult(
                errors=[violation],
                failed_chunk_ids=[c.chunk_id for c in chunks],
            )
        entities: list[Entity] = []
        triples: list[_RelTriple] = []
        ok_chunk_ids: set[str] = set()

        for chunk in chunks:
            entities.extend(self._scan_entities(chunk))
            triples.extend(self._scan_relations(chunk))
            ok_chunk_ids.add(chunk.chunk_id)

        # Endpoints referenced only by relations become entities.
        entities.extend(_materialize_endpoint_entities(triples, self._max_description))

        if self._resolve:
            entities = self._resolver.resolve_entities(entities)
        else:
            entities = _dedupe_entities(entities)

        name_index = _name_index(entities)
        relationships = _build_relationships(triples, name_index)

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            chunk_ids=sorted(ok_chunk_ids),
            failed_chunk_ids=[],
        )

    def _scan_entities(self, chunk: GraphChunk) -> list[Entity]:
        found: list[Entity] = []
        seen: set[tuple[str, str]] = set()

        def _add(name: str, etype: EntityType) -> None:
            key = (name.lower(), str(etype))
            if key in seen:
                return
            seen.add(key)
            found.append(
                Entity(
                    id=_node_id("ent", chunk.workspace_id, f"{etype}|{_norm(name)}"),
                    name=name,
                    type=etype,
                    description=_snippet(chunk.text, name, self._max_description),
                    workspace_id=chunk.workspace_id,
                    chunk_ids=[chunk.chunk_id],
                    document_ids=[chunk.document_id] if chunk.document_id else [],
                    properties={
                        "page": chunk.page,
                        "section": chunk.section,
                        "language": chunk.language,
                    },
                )
            )

        # Title-prefixed persons / two-word people-like mentions.
        for match in _TITLE_NAME_RE.finditer(chunk.text):
            name = match.group(1)
            etype = (
                EntityType.ORGANIZATION
                if _CORP_RE.search(name)
                else EntityType.PERSON
            )
            _add(name, etype)
        # Multi-word organization names ending in a corporate suffix.
        for match in re.finditer(rf"\b({_NAME} { _CORP})\b", chunk.text):
            _add(match.group(1), EntityType.ORGANIZATION)
        # Four-digit years as DATE entities.
        for year in _DATE_RE.findall(chunk.text):
            _add(year, EntityType.DATE)
        return found

    def _scan_relations(self, chunk: GraphChunk) -> list[_RelTriple]:
        found: list[_RelTriple] = []
        seen: set[tuple[str, str, str]] = set()
        for pattern, rtype, _shint, _thint in _REL_COMPILED:
            for match in pattern.finditer(chunk.text):
                src_name = match.group(1).strip()
                tgt_name = match.group(2).strip()
                if not src_name or not tgt_name:
                    continue
                key = (_norm(src_name), _norm(tgt_name), str(rtype))
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    _RelTriple(
                        source_name=src_name,
                        target_name=tgt_name,
                        relationship_type=rtype,
                        description=_snippet(chunk.text, src_name, self._max_description),
                        chunk=chunk,
                    )
                )
        return found



