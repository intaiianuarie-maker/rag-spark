"""Optional ask-time multimodal leg (``knowledge_multimodal`` page embeddings).

The worker (``MULTIMODAL_EMBED_ENABLED=true``) writes ONE point per rendered
PDF page into a separate 3072-d ``google/gemini-embedding-2`` collection. Here
the query TEXT is embedded with the same model (a text query lands in the shared
text/image space), that collection is searched, and each page-level hit is
mapped to the canonical TEXT chunks of that document version on that page.

Rules (locked by ``tests/unit/test_multimodal_retrieval_leg.py``):

- **Never fabricate evidence.** A point's own ``content`` is an image stub and is
  never returned. Only canonical ``chunks`` rows of the hit's version and page
  come back; a page with none is dropped.
- **Workspace is mandatory** and rechecked on every hit and every resolved row.
  The store's active-version gate (``documents.current_version_id`` /
  tombstone) and the resolver's lifecycle predicate both have to pass, so a
  deleted or superseded version cannot leak even if one layer is stale.
- Only ``workspace_ids`` and ``document_ids`` go to Qdrant; ``language`` and
  chunk ``metadata`` filters describe text chunks and are applied by the
  canonical resolver, not against page-point payloads that do not carry them.
- ``MultimodalNotRun`` is "nothing was attempted" (no key, collection absent);
  any other exception is a failed leg and the orchestrator degrades honestly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from .embedding_identity import provider_identity
from .interfaces import EmbeddingProvider, RetrievalMethod, RetrievedItem, SearchFilter

#: Page points requested from Qdrant per query (the page is the unit here).
DEFAULT_MULTIMODAL_TOP_K = 5

#: Canonical text chunks taken from one hit page, in document order. A page can
#: hold many chunks; the cap keeps one coarse visual hit from flooding fusion.
MAX_CHUNKS_PER_PAGE = 3

#: ``resolve_page_chunks``-shaped seam (``PostgresLexicalStore``): live canonical
#: chunks for ``(document_version_id, page)`` pairs inside the filter.
PageResolver = Callable[..., Awaitable[Sequence[RetrievedItem]]]


class MultimodalNotRun(Exception):
    """The leg was configured but could not run; ``reason`` is a stable token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MultimodalLeg:
    def __init__(
        self,
        *,
        store: Any,
        embedder: EmbeddingProvider | None,
        resolve_pages: PageResolver,
        collection: str,
        top_k: int = DEFAULT_MULTIMODAL_TOP_K,
        max_chunks_per_page: int = MAX_CHUNKS_PER_PAGE,
    ) -> None:
        self.store = store
        self.embedder = embedder  # None == no OpenRouter key: reported, never faked
        self.resolve_pages = resolve_pages
        self.collection = collection
        self.top_k = top_k
        self.max_chunks_per_page = max_chunks_per_page

    async def search(self, *, query: str, query_filter: SearchFilter) -> list[RetrievedItem]:
        allowed = {str(w) for w in query_filter.workspace_ids}
        if not allowed:
            return []
        if self.embedder is None:
            raise MultimodalNotRun("no_api_key")
        if not await self.store.collection_exists(self.collection):
            raise MultimodalNotRun("collection_missing")

        vector = await self.embedder.embed_query(query)
        hits = await self.store.search(
            collection=self.collection,
            vector=vector,
            top_k=self.top_k,
            query_filter=SearchFilter(
                workspace_ids=sorted(allowed), document_ids=query_filter.document_ids
            ),
            query_identity=provider_identity(self.embedder, len(vector)),
        )

        pages: dict[tuple[str, int], RetrievedItem] = {}
        for hit in hits:
            page = hit.page
            if page is None:
                page = (hit.metadata.get("visual_citation") or {}).get("page")
            version = hit.document_version_id
            if (
                not version
                or not isinstance(page, int)
                or isinstance(page, bool)
                or str(hit.workspace_id) not in allowed
                or (query_filter.document_ids and hit.document_id not in query_filter.document_ids)
            ):
                continue
            pages.setdefault((str(version), page), hit)
        if not pages:
            return []

        rows = await self.resolve_pages(pairs=list(pages), query_filter=query_filter)
        by_page: dict[tuple[str, int], list[RetrievedItem]] = {}
        for row in rows:
            if row.page is None:
                continue
            key = (str(row.document_version_id), row.page)
            hit = pages.get(key)
            if (
                hit is None
                or not row.chunk_id
                or str(row.workspace_id) not in allowed
                or row.document_id != hit.document_id
            ):
                continue
            by_page.setdefault(key, []).append(row)

        items: list[RetrievedItem] = []
        seen: set[str] = set()
        for key, hit in pages.items():  # hit order == Qdrant rank order
            for row in by_page.get(key, [])[: self.max_chunks_per_page]:
                if not row.chunk_id or row.chunk_id in seen:
                    continue
                seen.add(row.chunk_id)
                items.append(
                    row.model_copy(
                        update={
                            "score": hit.score,
                            "method": RetrievalMethod.DENSE,
                            "scores": {"multimodal": hit.score},
                            "metadata": {
                                **(row.metadata or {}),
                                "retrieval_modality": "visual",
                                "media_type": hit.metadata.get("media_type"),
                                "visual_citation": hit.metadata.get("visual_citation"),
                            },
                        }
                    )
                )
        return items
