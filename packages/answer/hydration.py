"""Bulk canonical hydration before context build (T-V1-022 · RP-03 / QF-03).

Verification and prompt context must see **authoritative passages**, never
payload echoes. This module is the single seam that turns a *candidate chunk
ref* (whatever a retriever happened to carry: dense payload, lexical payload,
graph description, fused item) into a **canonical record**:

* the **current** document version — the store adapters resolve the chunk
  through its document version and refuse rows whose document is ``deleted_at``
  or whose version is ``superseded_at`` (see
  :class:`~answer.adapters.postgres_lookup.PostgresChunkLookup`, which joins
  ``document_versions``/``documents`` and filters both);
* the **caller's scope** — the ref is dropped unless its workspace is inside
  ``allowed_workspace_ids`` (``None`` = public scope, existence only, exactly
  the rule :meth:`answer.engine.AnswerEngine.answer` applies to verification);
* the **original stored text** — retrieved summaries and payload text never
  override it: the canonical ``content`` replaces the item's ``content``;
* its **provenance** — document, filename, page, section, workspace.

Two seams share one :class:`CanonicalHydrator` (built in
:func:`answer.factory.build_answer_engine`):

1. **Context seam** — :class:`HydratingOrchestrator` hydrates the retrieved
   items *before* :func:`answer.prompts.build_prompt` runs, so the model only
   ever reads canonical passages. An item that cannot be hydrated (missing /
   deleted / superseded / foreign) or that carries no ref at all (a
   summary-only graph item) never reaches the prompt.
2. **Verify seam** — :func:`answer.verification.verify_claims` bulk-hydrates
   every cited ref up front, grounds against the canonical text, and performs a
   **fresh lifecycle revalidation immediately before returning**, so a
   document deleted while the LLM was generating loses: its citations are
   dropped at the door.

Graph descriptions stay **routing hints**: they decide *which* chunks are
candidates (:mod:`graph.retriever` marks them ``routing_hint``), they are
overwritten by canonical text before the prompt is built, and verification
never grounds a citation on them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from core.auth import ACLScope
from retrieval.interfaces import RetrievalMode, RetrievalOrchestrator, RetrievedItem

from .interfaces import ChunkLookup
from .models import Citation

logger = logging.getLogger("knowledge_os.answer.hydration")


def allowed_workspace_ids_for(acl_scope: ACLScope) -> set[str] | None:
    """Caller scope for hydration, mirroring ``AnswerEngine.answer``.

    Explicit workspace membership scopes the check; a caller that also sees
    public data passes existence-only (``None``).
    """
    if acl_scope.include_public:
        return None
    return set(acl_scope.workspace_ids)


def is_routing_hint(item: RetrievedItem) -> bool:
    """True when the item's own text is a *routing hint*, never evidence.

    Graph items carry a synthesized ``[Knowledge Graph Context]`` description
    (``is_graph``); any producer may also mark an item ``routing_hint``
    explicitly. Such text is never citable on its own — only the canonical
    hydration of its ref can make the ref citable.
    """
    metadata = item.metadata or {}
    return bool(metadata.get("is_graph") or metadata.get("routing_hint"))


@dataclass(frozen=True)
class HydratedChunk:
    """One canonically resolved chunk ref (current version + scope + text)."""

    chunk_id: str
    workspace_id: str | None = None
    document_id: str | None = None
    #: Present only when the lookup exposes it; the adapter's own
    #: "not superseded, not deleted" join is what makes the record current.
    document_version_id: str | None = None
    filename: str | None = None
    page: int | None = None
    section: str | None = None
    content: str | None = None

    @property
    def has_text(self) -> bool:
        """True when the store returned the original passage text."""
        return bool(self.content)

    @classmethod
    def from_citation(cls, citation: Citation) -> HydratedChunk:
        """Build the canonical record from a store-resolved citation."""
        return cls(
            chunk_id=citation.chunk_id,
            workspace_id=citation.workspace_id,
            document_id=citation.document_id,
            document_version_id=getattr(citation, "document_version_id", None),
            filename=citation.filename,
            page=citation.page,
            section=citation.section,
            content=citation.content,
        )


class CanonicalStoreUnavailable(RuntimeError):
    """Raised when canonical chunk hydration cannot reach the store.

    Missing/deleted/superseded rows are ordinary misses and stay ``None``;
    a database or query failure during the hydrating lookup is a different
    outcome that must never masquerade as "no citation".
    """


# Typing-friendly aliases (callers/tests may spell the name differently).
CanonicalStoreUnavailableError = CanonicalStoreUnavailable
StoreUnavailable = CanonicalStoreUnavailable
StoreUnavailableError = CanonicalStoreUnavailable

class CanonicalHydrator:
    """Bulk resolver: candidate chunk ref → canonical record.

    One instance is shared by the context seam and the verify seam, so a
    request performs at most one store read per ref per pass and exposes a
    single ``lookups`` counter — the T-V1-022 metric ("source lookups on the
    verify path: > 0").

    * ``hydrate`` resolves a whole batch of refs in one pass (deduplicated,
      order preserved) and returns **only the refs that resolved**; anything
      missing, deleted, superseded or foreign is simply absent, and the caller
      treats absence as rejection (fail closed).
    * Scope is re-checked on *every* read — including cache hits — so a shared
      hydrator can never hand one caller's chunk to another.
    * ``fresh=True`` bypasses and refreshes the cache. Verification uses it so
      the verify pass and the pre-return lifecycle revalidation always observe
      the *current* store state (a concurrent delete loses).
    * Implements the :class:`~answer.interfaces.ChunkLookup` protocol so it can
      be handed to ``AnswerEngine(chunk_lookup=...)`` unchanged.
    """

    def __init__(self, lookup: ChunkLookup | None) -> None:
        self._lookup = lookup
        self._cache: dict[str, HydratedChunk] = {}
        #: Refs actually resolved from the store (the T-V1-022 metric). Since
        #: T-V1-047 one store *pass* may cover a whole batch, this counts refs
        #: read rather than queries issued — 16 refs are 16 lookups in ONE
        #: query, not 16 queries.
        self.lookups = 0

    @property
    def available(self) -> bool:
        """True when a store was injected (hydration can actually run)."""
        return self._lookup is not None

    async def get(self, chunk_id: str) -> Citation | None:
        """Single-ref read; satisfies the ``ChunkLookup`` protocol."""
        if self._lookup is None:
            return None
        self.lookups += 1
        return await self._lookup.get(chunk_id)

    async def get_many(self, chunk_ids: Sequence[str]) -> dict[str, Citation]:
        """Bulk read; satisfies the ``ChunkLookup`` protocol (T-V1-047).

        One store pass for the whole batch, counted once per distinct ref in
        ``lookups`` — the same accounting :meth:`get` applies per call. No
        scope filtering here: exactly as with :meth:`get`, scoping is the
        caller's job (:meth:`hydrate` applies it), so the protocol surface
        stays a plain store read and a shared hydrator still cannot be used to
        widen a caller's scope.
        """
        if self._lookup is None:
            return {}
        ids = list(dict.fromkeys(chunk_ids))
        self.lookups += len(ids)
        return await self._lookup.get_many(ids)

    async def hydrate(
        self,
        refs: Iterable[str],
        *,
        allowed_workspace_ids: set[str] | None,
        fresh: bool = False,
    ) -> dict[str, HydratedChunk]:
        """Bulk-resolve ``refs`` to canonical records in one pass.

        Returns ``{chunk_id: HydratedChunk}`` for refs that resolved **and**
        are inside ``allowed_workspace_ids``; unresolvable refs are absent.
        With no store injected the result is empty and no read is issued.
        """
        if self._lookup is None:
            return {}
        resolved: dict[str, HydratedChunk] = {}
        missing: list[str] = []
        seen: set[str] = set()
        for ref in refs:
            if not ref or ref in seen:
                continue
            seen.add(ref)  # one read per distinct ref, cache hit or miss
            if not fresh and ref in self._cache:
                cached = self._cache[ref]
                if allowed_workspace_ids is None or cached.workspace_id in allowed_workspace_ids:
                    resolved[ref] = cached
                continue
            missing.append(ref)

        # T-V1-047: ONE store pass for the whole batch instead of one read per
        # ref (the N+1 that cost ~16 round-trips per /ask). `lookups` still
        # counts the refs read from the store — the T-V1-022 metric — so its
        # value is unchanged; only the number of queries collapses to one.
        found: dict[str, Citation] = {}
        if missing:
            self.lookups += len(missing)
            found = await self._lookup.get_many(missing)

        for ref in missing:
            citation = found.get(ref)
            if citation is None:
                # Missing / deleted / superseded: forget any earlier read.
                self._cache.pop(ref, None)
                continue
            if citation.workspace_id is None or (
                allowed_workspace_ids is not None
                and citation.workspace_id not in allowed_workspace_ids
            ):
                # Foreign (or unscoped) workspace: never citable, never cached.
                self._cache.pop(ref, None)
                continue
            chunk = HydratedChunk.from_citation(citation)
            self._cache[ref] = chunk
            resolved[ref] = chunk
        return resolved

    async def hydrate_items(
        self,
        items: Sequence[RetrievedItem],
        *,
        allowed_workspace_ids: set[str] | None,
    ) -> list[RetrievedItem]:
        """Context seam: canonicalise retrieved items **before context build**.

        * one canonical block per ref (duplicate payload/graph blocks collapse
          to the single authoritative passage),
        * canonical text and provenance replace the retrieved payload —
          summaries and payload text never override it,
        * items whose ref cannot be hydrated, or that carry no ref at all
          (summary-only routing hints), are dropped: they can never be cited,
          so they must never reach the model.

        With no store injected this is a pass-through (v1 behaviour).
        """
        if self._lookup is None:
            return list(items)

        refs = [item.chunk_id for item in items if item.chunk_id]
        resolved = await self.hydrate(refs, allowed_workspace_ids=allowed_workspace_ids)

        hydrated: list[RetrievedItem] = []
        seen: set[str] = set()
        for item in items:
            if not item.chunk_id:
                logger.info(
                    "hydration_dropped_refless_item",
                    extra={"event": "hydration_dropped_refless_item"},
                )
                continue
            if item.chunk_id in seen:
                continue
            chunk = resolved.get(item.chunk_id)
            if chunk is None or not chunk.has_text:
                logger.info(
                    "hydration_dropped_item",
                    extra={
                        "event": "hydration_dropped_item",
                        "chunk_id": item.chunk_id,
                        "reason": "unresolvable_or_textless",
                    },
                )
                continue
            seen.add(item.chunk_id)
            metadata = dict(item.metadata or {})
            metadata["canonical"] = True
            hydrated.append(
                item.model_copy(
                    update={
                        "content": chunk.content,
                        "workspace_id": chunk.workspace_id or item.workspace_id,
                        "document_id": chunk.document_id or item.document_id,
                        "document_version_id": (
                            chunk.document_version_id or item.document_version_id
                        ),
                        "page": chunk.page if chunk.page is not None else item.page,
                        "section": chunk.section or item.section,
                        "metadata": metadata,
                    }
                )
            )
        return hydrated


class HydratingOrchestrator(RetrievalOrchestrator):
    """Context seam: hydrate retrieved refs to canonical passages.

    Decorates the real orchestrator so that ``retrieve()`` returns items that
    have already been resolved to the current document version, the caller's
    scope, the original text and its provenance — i.e. hydration strictly
    **precedes** :func:`answer.prompts.build_prompt` (context build) and, one
    step later, :func:`answer.verification.verify_claims` (verify).

    All other attributes delegate to the wrapped orchestrator (``graph``,
    ``dense``, …) so callers cannot observe the decorator except by the
    canonicalised items it returns.
    """

    def __init__(self, inner: RetrievalOrchestrator, hydrator: CanonicalHydrator) -> None:
        self._inner = inner
        self._hydrator = hydrator

    def __getattr__(self, name: str):
        # Only reached when the normal lookup fails; never shadow _inner/_hydrator.
        try:
            inner = object.__getattribute__(self, "_inner")
        except AttributeError:
            raise AttributeError(name) from None
        return getattr(inner, name)

    async def retrieve(
        self,
        *,
        query: str,
        acl_scope: ACLScope,
        mode: RetrievalMode = RetrievalMode.HYBRID,
        **kwargs,
    ) -> list[RetrievedItem]:
        items = await self._inner.retrieve(query=query, acl_scope=acl_scope, mode=mode, **kwargs)
        return await self._hydrator.hydrate_items(
            items, allowed_workspace_ids=allowed_workspace_ids_for(acl_scope)
        )
