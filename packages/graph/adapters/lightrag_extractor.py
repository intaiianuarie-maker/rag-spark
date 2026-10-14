"""LightRAG-style LLM graph extractor (V2 work package G1).

Reuses :class:`models.interfaces.LLMProvider` (LiteLLM in production) behind
LightRAG's entity/relation extraction pattern: a small system prompt pins the
JSON schema, and one generation per chunk yields entities + relationships with
per-chunk provenance. No LightRAG source is copied verbatim; we adopt the
*pattern* (extract per-chunk, normalize to typed nodes, resolve, persist chunk
evidence) under our own dataclasses.

Failures are recorded as :class:`~graph.extractor.ExtractionError` and the chunk
is skipped — the extractor never raises on a single bad chunk.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from models.interfaces import (
    ChatMessage,
    GenerationRequest,
    GenerationResult,
    LLMProvider,
    MessageRole,
)

from ..extractor import (
    ExtractionError,
    ExtractionResult,
    GraphChunk,
    GraphExtractor,
    _build_relationships,
    _name_index,
    _node_id,
    _reject_mixed_workspace,
    _RelTriple,
)
from ..interfaces import Entity, EntityType, RelationshipType
from ..resolver import SimpleEntityResolver

_SYSTEM_PROMPT = """Extract knowledge graph entities and relationships from the text.
Return STRICT JSON only — no prose, no markdown fences.

Schema:
{"entities": [
  {
    "name": str,
    "type": "PERSON|ORGANIZATION|LOCATION|PRODUCT|CONCEPT|TECHNOLOGY|EVENT|DATE|CONTRACT|PROJECT",
    "description": str
  }
],
 "relationships": [
  {
    "source": str,
    "type": "WORKS_FOR|OWNS|SIGNED|LOCATED_IN|PART_OF|PRODUCES|DEPENDS_ON|MENTIONS|RELATED_TO",
    "target": str,
    "description": str
  }
]}
Entity names must be the canonical surface text exactly as it appears. Do not
invent names that are not in the source text. If nothing applies, emit
{"entities": [], "relationships": []}."""

_FENCE_RE = re.compile(r"^\s*(?:```+[a-zA-Z0-9_-]*)?\s*(\{.*\})\s*(?:```+)?\s*$", re.DOTALL)


def _loads_with_fence(text: str) -> Any:
    """Parse *text* as JSON, stripping a markdown fence if present."""
    match = _FENCE_RE.match(text)
    candidate = match.group(1) if match else text
    return json.loads(candidate)


def _parse_json(raw: str) -> dict[str, Any]:
    """Parse an LLM completion as a JSON **object**.

    Tolerates two common LLM output quirks:

    1. Markdown code fences wrapping the JSON (`` ```json ... ``` ``).
    2. Doubly-encoded JSON strings — some providers / test doubles wrap the
       completion text in quotes (``json.dumps(" ... ")``), so the first
       ``json.loads`` yields a ``str``; we unwrap up to two layers then re-parse.

    A non-object top-level value (string, number, array) is treated as a *parse*
    failure (``ValueError``) rather than a structural schema violation, so the
    caller maps it to ``LLM_PARSE_FAILED`` (consistent with the Q0 taxonomy
    where "I couldn't get a parseable graph object" ≠ a typed schema mismatch).
    """
    data = _loads_with_fence(raw.strip())
    # Unwrap doubly-encoded JSON strings (layer 1: provider quotes, layer 2:
    # any residual fencing inside the unwrapped string).
    for _ in range(2):
        if isinstance(data, str):
            data = _loads_with_fence(data.strip())
        else:
            break
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data


def _name_in_source(name: str, text: str) -> bool:
    """True when ``name`` is source-backed (not hallucinated by the model).

    Normalizes both strings (lowercase, punctuation/whitespace collapsed) and
    requires the **full contiguous token sequence** of the name to appear as a
    whole-word phrase in the text (order-sensitive).  This rejects:

    - Invented full names (``"Alice InventedSurname"`` — second token absent).
    - Substring false-positives (``"Ann"`` inside ``"Annual"``) — word boundaries enforced.
    - Recombined names (``"Alice Smith"`` from ``"Alice Jones met Bob Smith"`` — tokens present
      but not adjacent in the right order).
    - Reordered tokens (``"Smith Alice"`` where text has ``"Alice Smith"``).

    Single-token names must still appear as whole words.  Canonical aliases
    require explicit validated mappings by the extractor caller; bare aliases
    are not accepted.
    """
    collapsed = re.sub(r"[^\w\s]", " ", text.lower())
    collapsed = re.sub(r"\s+", " ", collapsed).strip()

    needle = re.sub(r"[^\w\s]", " ", name.lower())
    needle = re.sub(r"\s+", " ", needle).strip()
    if not needle:
        return False

    # Use word-boundary regex for contiguous, ordered phrase match.
    pattern = re.escape(needle)
    return re.search(r"\b" + pattern + r"\b", collapsed) is not None


class LLMBasedGraphExtractor(GraphExtractor):
    """Extract a graph via an LLMProvider, following LightRAG's pattern.

    The provider is injected (never a vendor SDK directly): in production a
    LiteLLM-backed :class:`LLMProvider` is used; in tests a fake provider that
    returns canned JSON is supplied.
    """

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        chunk_batch_size: int = 4,
        resolve: bool = True,
        max_description: int = 200,
        manual_aliases: dict[str, str] | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        if chunk_batch_size <= 0:
            raise ValueError("chunk_batch_size must be > 0")
        self._chunk_batch_size = chunk_batch_size
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
        errors: list[ExtractionError] = []
        ok_chunk_ids: list[str] = []
        failed_chunk_ids: list[str] = []

        # One generation per chunk so provenance stays exact (LightRAG extracts
        # per-chunk before the merge step).
        for i in range(0, len(chunks), self._chunk_batch_size):
            batch = chunks[i : i + self._chunk_batch_size]
            for chunk in batch:
                try:
                    result = await self._extract_one(chunk)
                except ExtractionError as exc:
                    # Malformed/unparsable/unverified output: record the error
                    # (with workspace+chunk provenance) and skip the chunk.
                    errors.append(exc)
                    continue
                except Exception as exc:  # provider/network — never per-chunk fatal
                    errors.append(
                        ExtractionError(
                            "LLM_EXTRACTION_FAILED",
                            str(exc),
                            workspace_id=chunk.workspace_id,
                            chunk_id=chunk.chunk_id,
                        )
                    )
                    continue
                # Only fully-verified chunks count as extracted provenance.
                ok_chunk_ids.append(chunk.chunk_id)
                entities.extend(result.entities)
                triples.extend(result.triples)

        if self._resolve:
            entities = self._resolver.resolve_entities(entities)
        else:
            entities = _dedupe_local(entities)
        name_index = _name_index(entities)
        relationships = _build_relationships(triples, name_index)

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            errors=errors,
            chunk_ids=sorted(set(ok_chunk_ids)),
            failed_chunk_ids=sorted(set(failed_chunk_ids)),
        )

    async def _extract_one(self, chunk: GraphChunk) -> _ExtractOneOutput:
        request = GenerationRequest(
            model=self._model,
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content=_SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=chunk.text),
            ],
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )

        completion: GenerationResult = await self._provider.generate(request)
        try:
            payload = _parse_json(completion.text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ExtractionError(
                "LLM_PARSE_FAILED",
                str(exc),
                workspace_id=chunk.workspace_id,
                chunk_id=chunk.chunk_id,
            ) from exc

        # _parse_json already guarantees a dict; remaining checks are real
        # schema-field validations (known types, source-backed names).
        raw_entities = payload.get("entities", []) or []
        raw_relationships = payload.get("relationships", []) or []
        if not isinstance(raw_entities, list) or not isinstance(
            raw_relationships, list
        ):
            raise ExtractionError(
                "LLM_SCHEMA_INVALID",
                "expected list 'entities' and 'relationships'",
                workspace_id=chunk.workspace_id,
                chunk_id=chunk.chunk_id,
            )

        known_entity_types = set(EntityType.__members__)
        known_rel_types = set(RelationshipType.__members__)

        # Every extracted name MUST be backed by the chunk text (no hallucinated
        # entities), and every type MUST be a known enum member -- unknown
        # relationship types are rejected rather than silently folded to RELATED_TO.
        entities: list[Entity] = []
        for item in raw_entities:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            raw_type = item.get("type", "")
            if not isinstance(raw_type, str) or raw_type.upper() not in known_entity_types:
                raise ExtractionError(
                    "LLM_SCHEMA_INVALID",
                    f"unknown entity type {raw_type!r} for name {name!r}",
                    workspace_id=chunk.workspace_id,
                    chunk_id=chunk.chunk_id,
                )
            if not _name_in_source(name, chunk.text):
                raise ExtractionError(
                    "EXTRACTION_UNVERIFIED",
                    f"entity name {name!r} not found in source text",
                    workspace_id=chunk.workspace_id,
                    chunk_id=chunk.chunk_id,
                )
            etype = EntityType(raw_type.upper())
            entities.append(
                Entity(
                    id=_node_id("ent", chunk.workspace_id, f"{etype}|{name.lower()}"),
                    name=name.strip(),
                    type=etype,
                    description=item.get("description", "")[: self._max_description],
                    workspace_id=chunk.workspace_id,
                    chunk_ids=[chunk.chunk_id],
                    document_ids=[chunk.document_id] if chunk.document_id else [],
                )
            )

        triples: list[_RelTriple] = []
        for item in raw_relationships:
            if not isinstance(item, dict):
                continue
            src = item.get("source")
            tgt = item.get("target")
            if not isinstance(src, str) or not isinstance(tgt, str):
                continue
            raw_rtype = item.get("type", "")
            if not isinstance(raw_rtype, str) or raw_rtype.upper() not in known_rel_types:
                raise ExtractionError(
                    "LLM_SCHEMA_INVALID",
                    f"unknown relationship type {raw_rtype!r}",
                    workspace_id=chunk.workspace_id,
                    chunk_id=chunk.chunk_id,
                )
            if not _name_in_source(src, chunk.text) or not _name_in_source(
                tgt, chunk.text
            ):
                raise ExtractionError(
                    "EXTRACTION_UNVERIFIED",
                    f"relation endpoint {src!r}->{tgt!r} not found in source text",
                    workspace_id=chunk.workspace_id,
                    chunk_id=chunk.chunk_id,
                )
            rtype = RelationshipType(raw_rtype.upper())
            triples.append(
                _RelTriple(
                    source_name=src.strip(),
                    target_name=tgt.strip(),
                    relationship_type=rtype,
                    description=item.get("description", "")[: self._max_description],
                    chunk=chunk,
                )
            )

        return _ExtractOneOutput(entities=entities, triples=triples)


@dataclass(slots=True)
class _ExtractOneOutput:
    entities: list[Entity]
    triples: list[_RelTriple]


def _dedupe_local(entities: Sequence[Entity]) -> list[Entity]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Entity] = []
    for ent in entities:
        key = (ent.workspace_id, ent.name.lower(), str(ent.type).upper())
        if key in seen:
            continue
        seen.add(key)
        out.append(ent)
    return out

