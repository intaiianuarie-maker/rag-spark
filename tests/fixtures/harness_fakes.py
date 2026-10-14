"""Test-only fakes and reference helpers for the M0-004 contract suite.

Nothing in this module ships as product code — it lives under `tests/` and
implements the *interfaces* declared in `packages/*/interfaces.py` so the
contract can be locked before M1-M3 adapters exist (TASK-M0-004: "all against
stub/fake impls"). Two entries are labelled references rather than fakes:

- :func:`reference_rrf` — a numerically pinned RRF implementation used to lock
  the determinism contract M3-001b must satisfy.
- :class:`ReferenceHybridOrchestrator` — a minimal ACL-aware orchestrator used
  to lock "ACL filter applied BEFORE ranking" for M3-001b.

Both are deliberately ~40 lines each: the product orchestrator is the only one
that may grow, and it must satisfy the very same assertions.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator, Sequence
from typing import Any

from connectors.interfaces import (
    Connector,
    ConnectorConfig,
    ConnectorDiscovery,
    ConnectorKind,
    ConnectorSyncResult,
    SourceItem,
)
from core.auth import ACLScope
from ingestion.interfaces import DocumentElement, DocumentParser, ParsedDocument
from models.interfaces import (
    GenerationRequest,
    GenerationResult,
    LLMProvider,
)
from retrieval.interfaces import (
    EmbeddingProvider,
    LexicalStore,
    Reranker,
    RetrievalMethod,
    RetrievalOrchestrator,
    RetrievedItem,
    SearchFilter,
    VectorRecord,
    VectorStore,
)

RRF_K = 60


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def make_record(
    record_id: str,
    workspace_id: str,
    content: str,
    *,
    vector: Sequence[float] | None = None,
    document_id: str = "doc-1",
    page: int | None = None,
    section: str | None = None,
) -> VectorRecord:
    """A fully-populated VectorRecord (provenance on, so citation fields are checked)."""
    return VectorRecord(
        id=record_id,
        vector=list(vector) if vector is not None else [1.0, 0.0, 0.0, 0.0],
        content=content,
        workspace_id=workspace_id,
        document_id=document_id,
        document_version_id=f"{document_id}-v1",
        chunk_id=record_id,
        page=page,
        section=section,
    )


def make_item(
    content: str,
    score: float = 0.5,
    *,
    workspace_id: str = "ws-a",
    document_id: str = "doc-1",
    chunk_id: str | None = None,
    page: int | None = None,
    section: str | None = None,
    method: RetrievalMethod = RetrievalMethod.DENSE,
) -> RetrievedItem:
    """A fully-populated RetrievedItem (citation fields on) for contract tests."""
    return RetrievedItem(
        content=content,
        score=score,
        method=method,
        workspace_id=workspace_id,
        document_id=document_id,
        document_version_id=f"{document_id}-v1",
        chunk_id=chunk_id or content[:16],
        page=page,
        section=section,
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError(f"vector dimension mismatch: {len(left)} != {len(right)}")
    numerator = math.fsum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.fsum(a * a for a in left)
    right_norm = math.fsum(b * b for b in right)
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (math.sqrt(left_norm) * math.sqrt(right_norm))


def _in_scope(record: VectorRecord, query_filter: SearchFilter) -> bool:
    """Hard workspace filter — the store's half of R-004 (fail closed)."""
    if record.workspace_id not in query_filter.workspace_ids:
        return False
    if query_filter.document_ids is not None:
        if record.document_id not in query_filter.document_ids:
            return False
    if query_filter.language is not None:
        if record.metadata.get("language") != query_filter.language:
            return False
    return all(record.metadata.get(key) == value for key, value in query_filter.metadata.items())


def _to_item(record: VectorRecord, score: float, method: RetrievalMethod) -> RetrievedItem:
    return RetrievedItem(
        content=record.content,
        score=score,
        method=method,
        workspace_id=record.workspace_id,
        document_id=record.document_id,
        document_version_id=record.document_version_id,
        chunk_id=record.chunk_id,
        page=record.page,
        section=record.section,
        scores={method.value: score},
        metadata=dict(record.metadata),
    )


# --------------------------------------------------------------------------------------
# Store fakes
# --------------------------------------------------------------------------------------


class FakeVectorStore(VectorStore):
    """In-memory VectorStore that enforces the ACL filter *before* ranking.

    `last_search_filter` / `search_calls` let the ACL-before-rank tests prove
    the filter arrived with the call (not retrofitted after ranking).
    """

    def __init__(self, *, dimension: int = 4) -> None:
        self.dimension = dimension
        self.collections: dict[str, dict[str, VectorRecord]] = {}
        self.search_calls: list[SearchFilter] = []

    async def ensure_collection(
        self,
        *,
        collection: str,
        dimension: int,
        distance: str = "cosine",
        recreate: bool = False,
    ) -> None:
        if dimension <= 0:
            raise ValueError(f"dimension must be positive, got {dimension}")
        if recreate or collection not in self.collections:
            if self.collections.get(collection):
                existing = next(iter(self.collections[collection].values()))
                if len(existing.vector) != dimension:
                    raise ValueError(
                        f"collection {collection} already has dimension {len(existing.vector)}"
                    )
            self.collections[collection] = {}
        self.dimension = dimension

    async def upsert(self, *, collection: str, records: Sequence[VectorRecord]) -> int:
        if collection not in self.collections:
            raise KeyError(f"collection {collection} does not exist; call ensure_collection first")
        for record in records:
            if len(record.vector) != self.dimension:
                raise ValueError(
                    f"record {record.id} has dimension {len(record.vector)}, "
                    f"collection expects {self.dimension}"
                )
        self.collections[collection].update({record.id: record for record in records})
        return len(records)

    async def search(
        self,
        *,
        collection: str,
        vector: list[float],
        top_k: int,
        query_filter: SearchFilter,
        query_identity=None,
    ) -> list[RetrievedItem]:
        self.search_calls.append(query_filter)
        if top_k <= 0:
            return []
        if not query_filter.workspace_ids:
            return []  # fail closed: an empty workspace scope may not rank anything
        records = self.collections.get(collection, {})
        scored = [
            (_cosine(vector, record.vector), record.id, record)
            for record in records.values()
            if _in_scope(record, query_filter)
        ]
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [
            _to_item(record, score, RetrievalMethod.DENSE)
            for score, _, record in scored[:top_k]
        ]

    async def delete(
        self,
        *,
        collection: str,
        ids: Sequence[str] | None = None,
        query_filter: SearchFilter | None = None,
    ) -> int:
        records = self.collections.get(collection, {})
        if ids is not None:
            removed = sum(1 for record_id in ids if records.pop(record_id, None) is not None)
            return removed
        if query_filter is None:
            raise ValueError("delete requires ids or query_filter")
        doomed = [record.id for record in records.values() if _in_scope(record, query_filter)]
        for record_id in doomed:
            del records[record_id]
        return len(doomed)


class FakeLexicalStore(LexicalStore):
    """In-memory token-overlap FTS fake with the same fail-closed ACL filter."""

    def __init__(self) -> None:
        self.records: dict[str, VectorRecord] = {}
        self.search_calls: list[SearchFilter] = []

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token for token in text.lower().split() if token.isalnum() or len(token) > 2}

    async def index(self, *, records: Sequence[VectorRecord]) -> int:
        self.records.update({record.id: record for record in records})
        return len(records)

    async def search(
        self,
        *,
        query: str,
        top_k: int,
        query_filter: SearchFilter,
    ) -> list[RetrievedItem]:
        self.search_calls.append(query_filter)
        if top_k <= 0 or not query_filter.workspace_ids:
            return []
        query_tokens = self._tokens(query)
        scored = []
        for record in self.records.values():
            if not _in_scope(record, query_filter):
                continue
            overlap = len(query_tokens & self._tokens(record.content))
            if overlap:
                scored.append((overlap / max(len(query_tokens), 1), record.id, record))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [
            _to_item(record, score, RetrievalMethod.LEXICAL)
            for score, _, record in scored[:top_k]
        ]

    async def delete(
        self,
        *,
        ids: Sequence[str] | None = None,
        query_filter: SearchFilter | None = None,
    ) -> int:
        if ids is not None:
            removed = sum(1 for record_id in ids if self.records.pop(record_id, None) is not None)
            return removed
        if query_filter is None:
            raise ValueError("delete requires ids or query_filter")
        doomed = [record.id for record in self.records.values() if _in_scope(record, query_filter)]
        for record_id in doomed:
            del self.records[record_id]
        return len(doomed)


# --------------------------------------------------------------------------------------
# Model / reranker / parser / connector fakes
# --------------------------------------------------------------------------------------


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic, weight-free embeddings (hash-derived vectors)."""

    model_name = "fake-embed"
    dimension = 8
    revision = "fake-embed-1"

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [byte / 255.0 for byte in digest[: FakeEmbeddingProvider.dimension]]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        for text in texts:
            if not isinstance(text, str):
                raise ValueError(f"expected str, got {type(text).__name__}")
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        if not text:
            raise ValueError("query text must be non-empty")
        return self._vector(text)


class FakeReranker(Reranker):
    """Token-overlap reranker: no weights, no network, no mutation of the input."""

    model_name = "fake-reranker"

    @staticmethod
    def _overlap(query: str, content: str) -> float:
        query_tokens = set(query.lower().split())
        content_tokens = set(content.lower().split())
        if not query_tokens or not content_tokens:
            return 0.0
        return len(query_tokens & content_tokens) / len(query_tokens)

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[RetrievedItem],
        top_k: int | None = None,
    ) -> list[RetrievedItem]:
        scored = [item.model_copy(deep=True) for item in documents]
        for item in scored:
            item.score = self._overlap(query, item.content)
            item.method = RetrievalMethod.RERANKED
            item.scores = {**item.scores, "reranker": item.score}
        scored.sort(key=lambda item: (-item.score, item.content))
        return scored if top_k is None else scored[:top_k]


class FakeDocumentParser(DocumentParser):
    """Returns caller-supplied elements: locks the *interface*, never the parsing."""

    supported_mime_types = frozenset(
        {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
            "text/html",
            "text/markdown",
            "text/csv",
            "text/plain",
        }
    )

    def __init__(self, elements: Sequence[DocumentElement], *, language: str = "en") -> None:
        self.elements = list(elements)
        self.language = language

    async def parse(
        self,
        data: bytes,
        *,
        mime_type: str,
        filename: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ParsedDocument:
        if mime_type not in self.supported_mime_types:
            raise ValueError(f"unsupported mime_type for FakeDocumentParser: {mime_type}")
        if not data:
            raise ValueError("empty input: refusing to fabricate an empty document")
        page_count = max((element.page or 0) for element in self.elements) or None
        return ParsedDocument(
            elements=list(self.elements),
            language=self.language,
            mime_type=mime_type,
            page_count=page_count,
            metadata={"filename": filename, **(metadata or {})},
        )


class FakeConnector(Connector):
    """In-memory connector over a fixed item list (discover/fetch/sync shape)."""

    kind = ConnectorKind.LOCAL_UPLOAD

    def __init__(self, items: Sequence[SourceItem]) -> None:
        self.items = list(items)
        self.blobs: dict[str, bytes] = {
            item.external_id: item.name.encode("utf-8") for item in items
        }

    async def discover(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorDiscovery:
        start = int(cursor or 0)
        window = self.items[start : start + 2]
        next_cursor = str(start + 2) if start + 2 < len(self.items) else None
        return ConnectorDiscovery(items=window, next_cursor=next_cursor)

    async def fetch(self, config: ConnectorConfig, item: SourceItem) -> bytes:
        if item.external_id not in self.blobs:
            raise KeyError(f"unknown external_id: {item.external_id}")
        return self.blobs[item.external_id]

    async def sync(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorSyncResult:
        discovered = await self.discover(config, cursor)
        fetched = 0
        for item in discovered.items:
            await self.fetch(config, item)
            fetched += 1
        return ConnectorSyncResult(discovered=len(discovered.items), fetched=fetched)


class FakeLLMProvider(LLMProvider):
    """Echo provider: proves generate/stream shapes without weights or keys."""

    provider_name = "fake-llm"

    @staticmethod
    def _respond(request: GenerationRequest) -> str:
        prompt = next(
            (message.content for message in reversed(request.messages) if message.content),
            "",
        )
        return f"echo: {prompt}"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if not request.model:
            raise ValueError("request.model is required (model passthrough contract)")
        return GenerationResult(
            text=self._respond(request),
            model=request.model,
            finish_reason="stop",
            prompt_tokens=0,
            completion_tokens=len(self._respond(request).split()),
        )

    def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        text = self._respond(request)

        async def generator() -> AsyncIterator[str]:
            # Chunks must concatenate back into exactly the generated text.
            words = text.split(" ")
            for index, word in enumerate(words):
                yield word if index == len(words) - 1 else f"{word} "

        return generator()


# --------------------------------------------------------------------------------------
# Reference implementations (contract anchors for M3-001b)
# --------------------------------------------------------------------------------------


def reference_rrf(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal-rank fusion: score(d) = Σ 1/(k + rank) across rankings.

    Determinism contract (asserted by `check_rrf_determinism`):
    identical input ⇒ identical output, *and* reordering the input rankings or
    the ids inside a ranking never changes a document's fused score
    (`math.fsum` makes the accumulation order-independent and exactly rounded).
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    contributions: dict[str, list[float]] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            contributions.setdefault(doc_id, []).append(1.0 / (k + rank))
    return {doc_id: math.fsum(values) for doc_id, values in contributions.items()}


def sort_fused(scores: dict[str, float]) -> list[str]:
    """Deterministic ranking: score desc, then id asc (no dict-order dependence)."""
    return sorted(scores, key=lambda doc_id: (-scores[doc_id], doc_id))


class ReferenceHybridOrchestrator(RetrievalOrchestrator):
    """TEST-ONLY ACL-aware orchestrator (M3-001b must satisfy these assertions).

    Order of operations is the contract:
    `ACLScope → SearchFilter → retriever calls (filter applied inside the store,
    before ranking) → RRF fusion → truncate`.
    """

    def __init__(
        self,
        dense: VectorStore,
        lexical: LexicalStore,
        *,
        collection: str = "contract",
        embedding: EmbeddingProvider | None = None,
    ) -> None:
        self.dense = dense
        self.lexical = lexical
        self.collection = collection
        self.embedding = embedding or FakeEmbeddingProvider()

    async def retrieve(
        self,
        *,
        query: str,
        acl_scope: ACLScope,
        dense_top_k: int = 30,
        lexical_top_k: int = 30,
        fusion_top_k: int = 20,
        rerank_top_k: int = 10,
        context_top_k: int = 6,
    ) -> list[RetrievedItem]:
        if acl_scope.is_empty:
            return []  # fail closed: no workspaces, no ranking
        query_filter = SearchFilter(workspace_ids=list(acl_scope.workspace_ids))
        vector = await self.embedding.embed_query(query)
        dense_items = await self.dense.search(
            collection=self.collection,
            vector=vector,
            top_k=dense_top_k,
            query_filter=query_filter,
        )
        lexical_items = await self.lexical.search(
            query=query, top_k=lexical_top_k, query_filter=query_filter
        )

        def key(item: RetrievedItem) -> str:
            return item.chunk_id or item.content

        fused = reference_rrf(
            [[key(item) for item in dense_items], [key(item) for item in lexical_items]]
        )
        by_key = {key(item): item for item in [*dense_items, *lexical_items]}
        ranked = sort_fused(fused)[:fusion_top_k]
        results = []
        for doc_id in ranked:
            item = by_key[doc_id].model_copy(deep=True)
            item.method = RetrievalMethod.FUSED
            item.scores = {**item.scores, "rrf": fused[doc_id]}
            item.score = fused[doc_id]
            results.append(item)
        return results
