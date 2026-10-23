# ruff: noqa: E501
"""Community detection and summarization for Global GraphRAG (T-V2-007).

Community detection: connected components over entity/relationship graph.
No new deps: uses Python stdlib BFS with sorted order for determinism.

Summarization: per-community LLM summary via the approved Gemma route with a
paid fallback policy (T-OR-13). The provider is built through the model router
so the OpenRouter transport applies free-first → paid-on-exhaustion at the
network layer; a summary failure never aborts the job.

Global retrieval already exists in GraphRetriever.global_search; this module
provides the build-time detection and summarization that populates the
communities the retriever ranks.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque

from .interfaces import Entity, GraphCommunity, Relationship

# Bounded summary cap: 500 chars, ~100 tokens, within max_tokens
SUMMARY_MAX_CHARS = 500
SUMMARY_MAX_TOKENS = 1000  # reasoning models need >=1000 or they return empty

# Approved Gemma route for summarization (T-OR-13). Resolved through the model
# router via ``graph.dispatch.gemma_provider`` so the free-first → paid-on-
# exhaustion fallback is applied by the transport; free is preferred and paid
# only escalates on pool exhaustion.
SUMMARY_MODEL = "google/gemma-4-26b-a4b-it:free"
SUMMARY_PAID_MODEL = "google/gemma-4-26b-a4b-it"


def _community_id(workspace_id: str, member_ids: list[str]) -> str:
    """Deterministic community id from sorted member ids."""
    digest = hashlib.sha256(f"{workspace_id}|{','.join(sorted(member_ids))}".encode()).hexdigest()[:12]
    return f"comm-{digest}"


def detect_communities(
    entities: list[Entity],
    relationships: list[Relationship],
    workspace_id: str,
) -> list[GraphCommunity]:
    """Detect communities as connected components (deterministic).

    No new graph-library deps: BFS over adjacency list, sorted order for
    determinism (same graph → same communities). Each component with >=2
    entities becomes a community; singletons are not communities (they are
    already retrievable via entity search).

    Algorithm choice: connected components is the minimal viable community
    detection that needs nothing beyond Python. Louvain/leiden would require
    `community` or `leiden` libs, not in tree, and would add a heavy dependency
    for marginal gain on small graphs. Connected components captures the core
    signal: entities linked by relationships form a theme. Determinism is
    proven by double-run test (same input → same output, sorted).
    """
    if not entities or not relationships:
        return []

    # Build adjacency: entity_id -> set of neighbor entity_ids
    adj: dict[str, set[str]] = defaultdict(set)
    entity_ids = {e.id for e in entities if e.workspace_id == workspace_id}
    for rel in relationships:
        if rel.workspace_id != workspace_id:
            continue
        if rel.source_entity_id in entity_ids and rel.target_entity_id in entity_ids:
            adj[rel.source_entity_id].add(rel.target_entity_id)
            adj[rel.target_entity_id].add(rel.source_entity_id)

    # Include isolated entities as singleton components, but filter later
    for eid in entity_ids:
        adj.setdefault(eid, set())

    visited: set[str] = set()
    communities: list[GraphCommunity] = []

    # Sorted for determinism
    for start in sorted(entity_ids):
        if start in visited:
            continue
        # BFS
        component: set[str] = set()
        queue = deque([start])
        visited.add(start)
        while queue:
            cur = queue.popleft()
            component.add(cur)
            for nb in sorted(adj[cur]):
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)

        # Only components with >=2 entities are communities
        if len(component) < 2:
            continue

        comp_entities = sorted(component)
        # Collect relationships within component
        comp_rels = [
            r.id
            for r in relationships
            if r.source_entity_id in component and r.target_entity_id in component
        ]
        # Collect chunk_ids from member entities/relationships
        chunk_ids: set[str] = set()
        for e in entities:
            if e.id in component:
                chunk_ids.update(e.chunk_ids)
        for r in relationships:
            if r.id in comp_rels:
                chunk_ids.update(r.chunk_ids)

        # Deterministic community id
        cid = _community_id(workspace_id, comp_entities)

        # Placeholder summary; will be replaced by LLM summary
        # Provenance is complete from the start, even before summary
        communities.append(
            GraphCommunity(
                id=cid,
                workspace_id=workspace_id,
                title=f"Community {cid[:8]}",
                summary="",  # to be filled by summarize
                entity_ids=comp_entities,
                relationship_ids=sorted(comp_rels),
            )
        )
        # Store chunk provenance in properties for later hydration if needed
        # (GraphCommunity doesn't have chunk_ids field, so we use entity/relationship
        # provenance; the store's community payload will carry it)
        # For now, we keep it simple: entity_ids + relationship_ids are the provenance

    # Sort communities by id for determinism
    communities.sort(key=lambda c: c.id)
    return communities


async def summarize_community(
    community: GraphCommunity,
    entities: list[Entity],
    relationships: list[Relationship],
    workspace_id: str,
) -> str:
    """Generate a bounded summary for a community via the approved Gemma route.

    Uses the approved Gemma route (``google/gemma-4-26b-a4b-it:free`` FIRST,
    with paid fallback to ``google/gemma-4-26b-a4b-it``) resolved through the
    model router via :func:`graph.dispatch.gemma_provider`. The OpenRouter
    transport applies free-first to paid-on-exhaustion at the network layer, so
    a free-pool exhaustion escalates to paid without the caller assembling a paid
    route by hand. ``max_tokens`` is bounded to :data:`SUMMARY_MAX_TOKENS`
    (reasoning models need >=1000 or they return empty). Summary is bounded to
    :data:`SUMMARY_MAX_CHARS` and stored with provenance (member ids).

    If the LLM is unavailable, returns a deterministic fallback (joined entity
    names) so extraction still succeeds; the job does not fail for a summary
    miss.
    """
    # Bound check: if community has no members, return empty
    if not community.entity_ids:
        return ""

    # Build prompt: list entities and relationships
    member_names = [e.name for e in entities if e.id in community.entity_ids]
    rel_descriptions = []
    for rel in relationships:
        if rel.id in community.relationship_ids:
            # Find source/target names
            src = next((e.name for e in entities if e.id == rel.source_entity_id), rel.source_entity_id)
            tgt = next((e.name for e in entities if e.id == rel.target_entity_id), rel.target_entity_id)
            rel_descriptions.append(f"{src} -[{rel.type}]-> {tgt}: {rel.description}".strip())

    # Deterministic fallback so extraction still succeeds if the LLM misses
    fallback = f"Entities: {', '.join(member_names[:5])}. Relationships: {'; '.join(rel_descriptions[:3])}"
    fallback = fallback[:SUMMARY_MAX_CHARS]

    try:
        from models.interfaces import ChatMessage, GenerationRequest, GenerationResult, MessageRole

        from .dispatch import gemma_provider

        # Resolve the approved Gemma route: free-first with a paid fallback policy
        # applied by the OpenRouter transport. provider is built via the model
        # router and escalates free to paid on free-pool exhaustion.
        provider, summary_model, _paid_model = gemma_provider()
        # The transport applies the paid fallback on free-pool exhaustion; we
        # only ever request the free route name here.
        assert "free" in summary_model, (
            "community summary model must be the Gemma free route (paid fallback "
            "is handled by the transport, not requested by name)"
        )

        prompt = (
            f"Summarize this community of entities and relationships in one concise paragraph "
            f"(under {SUMMARY_MAX_CHARS} characters) for global retrieval. Focus on the common theme.\n\n"
            f"Entities: {', '.join(member_names)}\n"
            f"Relationships: {'; '.join(rel_descriptions)}\n\n"
            f"Summary:"
        )
        request = GenerationRequest(
            model=summary_model,
            messages=[ChatMessage(role=MessageRole.USER, content=prompt)],
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        result: GenerationResult = await provider.generate(request)
        summary = (result.text or "").strip()
        # Reasoning model returned empty without enough max_tokens - fallback
        if not summary:
            return fallback
        # Enforce bound
        if len(summary) > SUMMARY_MAX_CHARS:
            summary = summary[: SUMMARY_MAX_CHARS - 1] + "\u2026"
        return summary
    except Exception:
        # LLM unavailable (free-pool exhausted and paid fallback unreachable, or
        # no provider configured) -> deterministic fallback so the job still
        # succeeds for a summary miss; the community is still emitted with its
        # provenance (member ids).
        return fallback
