"""Contract checks for the retrieval adapter interfaces (`retrieval.interfaces`).

Test-only (TASK-M0-004 allows `packages/*/contract_tests.py` placeholders): these
are plain async functions with explicit ``AssertionError``s so they can be
imported by `tests/contracts/**` *and* by future adapter tests (M3) without a
pytest dependency. Every check runs against whatever implementation you pass —
fakes today, the real Qdrant/Postgres/bge adapters from M3 onwards — so an
adapter swap cannot silently change the ACL/provenance guarantees.

Locked invariants (see `docs/rag/retrieval-spec.md`, R-004, citation-spec):

1. Workspace scope is applied **inside the store, before ranking** — an
   out-of-scope row with a perfect score must still be unreachable.
2. An empty workspace scope returns nothing (fail closed).
3. Provenance (document/version/chunk/page/section) survives every hop.
4. Fusion is deterministic and order-independent.
5. **Lifecycle belongs to the caller**: these checkers never close a store, so a
   caller that closes in `finally` cannot double-close (code1 review Minor 1).

**v1 assumption — `ACLScope.include_public`.** `SearchFilter` carries
`workspace_ids` only, and the harness's reference orchestrator translates an
`ACLScope` into exactly that list, so `include_public` is *not* propagated
today. v1 has no public documents: a public-only scope therefore fails closed
(zero results) rather than silently ranking everything. When M3 introduces
public visibility it must either thread `include_public` through
`SearchFilter` and have stores honour it, or drop the field from `ACLScope` —
either way that is a contract change to be reflected here, not a silent
behaviour change (code1 review Minor 3).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping, Sequence

from .embedding_identity import EmbeddingIdentity
from .interfaces import (
    EmbeddingProvider,
    LexicalStore,
    Reranker,
    RetrievedItem,
    SearchFilter,
    VectorRecord,
    VectorStore,
)

CONTRACT_COLLECTION = "contract-tests"
CONTRACT_DIMENSION = 4
#: T-R06b: the boundary now refuses records with no embedding identity, so the
#: contract's own fixture records carry one. Same four keys, built with dev2's
#: stamping helpers — no parallel implementation here.
CONTRACT_IDENTITY = EmbeddingIdentity(
    provider="ContractStub",
    model="contract-stub-model",
    revision="contract-stub-rev1",
    dimension=CONTRACT_DIMENSION,
)


def _fail(message: str) -> None:
    raise AssertionError(message)


def _require_instance(value: object, expected_type: type, what: str) -> None:
    """Runtime leak guard (module-boundaries): static types can't catch a casted return."""
    if not isinstance(value, expected_type):
        _fail(f"{what}: expected {expected_type.__name__}, got {type(value).__name__}")


async def _expect_error(awaitable: Awaitable[object], what: str) -> None:
    """Invalid input must raise loudly (the exception type is the adapter's choice)."""
    try:
        result = await awaitable
    except Exception:  # any loud failure satisfies the contract
        return
    _fail(f"{what}: expected an exception, but returned {result!r}")


def _scope(*workspace_ids: str) -> SearchFilter:
    return SearchFilter(workspace_ids=list(workspace_ids))


async def check_vector_store_contract(
    store: VectorStore,
    *,
    collection: str = CONTRACT_COLLECTION,
    dimension: int = CONTRACT_DIMENSION,
) -> None:
    """insert / search / filter-by-workspace / delete round-trip shape.

    Lifecycle is owned by the *caller* (construct → check → close in
    ``finally``); this checker deliberately never closes ``store``, so a real
    adapter cannot be double-closed.
    """
    await store.ensure_collection(collection=collection, dimension=dimension)

    base = [1.0] + [0.0] * (dimension - 1)
    slight = [0.9, 0.1] + [0.0] * (dimension - 2) if dimension >= 2 else base
    records = [
        VectorRecord(
            id="a-1",
            vector=base,
            content="alpha evidence about permissions",
            workspace_id="ws-a",
            document_id="doc-a",
            document_version_id="doc-a-v1",
            chunk_id="chunk-a-1",
            page=1,
            section="Introduction",
            metadata=CONTRACT_IDENTITY.as_metadata(),
        ),
        VectorRecord(
            id="a-2",
            vector=slight,
            content="alpha second paragraph",
            workspace_id="ws-a",
            document_id="doc-a",
            document_version_id="doc-a-v1",
            chunk_id="chunk-a-2",
            page=2,
            section="Method",
            metadata=CONTRACT_IDENTITY.as_metadata(),
        ),
        # Same vector as the query: if ACL ran after ranking this row would win.
        VectorRecord(
            id="b-1",
            vector=base,
            content="bravo secret phrase",
            workspace_id="ws-b",
            document_id="doc-b",
            document_version_id="doc-b-v1",
            chunk_id="chunk-b-1",
            page=1,
            section="Secret",
            metadata=CONTRACT_IDENTITY.as_metadata(),
        ),
    ]

    written = await store.upsert(collection=collection, records=records)
    if written != 3:
        _fail(f"upsert must return the number written (expected 3, got {written})")

    if await store.upsert(collection=collection, records=[]) != 0:
        _fail("upsert of an empty batch must return 0 (no-op), not raise")

    hits = await store.search(
        collection=collection,
        vector=list(base),
        top_k=10,
        query_filter=_scope("ws-a"),
        # T-A05: the dense boundary requires an explicit query identity; the
        # contract's fixture records carry CONTRACT_IDENTITY, so a matching
        # query verifies compatibility instead of assuming it.
        query_identity=CONTRACT_IDENTITY,
    )
    if not hits:
        _fail("search returned no hits for its own workspace")
    for hit in hits:
        _require_instance(hit, RetrievedItem, "search return item")
        if hit.workspace_id != "ws-a":
            _fail(f"ACL applied after ranking: leaked {hit.workspace_id} ({hit.content!r})")
        if hit.document_id is None or hit.chunk_id is None:
            _fail(f"citation provenance lost on {hit}")
        if hit.page is None or hit.section is None:
            _fail(f"page/section provenance lost on {hit}")
        if not math.isfinite(hit.score):
            _fail(f"score must be a finite float, got {hit.score!r}")
        if not hit.content:
            _fail("retrieved item lost its content")

    if len(hits) > 10:
        _fail(f"top_k=10 ignored: {len(hits)} items returned")

    one = await store.search(
        collection=collection, vector=list(base), top_k=1,
        query_filter=_scope("ws-a"), query_identity=CONTRACT_IDENTITY,
    )
    if len(one) != 1:
        _fail(f"top_k=1 must return exactly one item, got {len(one)}")

    if await store.search(
        collection=collection, vector=list(base), top_k=5,
        query_filter=_scope(), query_identity=CONTRACT_IDENTITY,
    ):
        # Fail closed (R-004). v1 assumption: `SearchFilter` has no
        # `include_public` field yet, so this is also the behaviour a
        # public-only ACLScope produces — see the module docstring.
        _fail("empty workspace scope must return nothing (fail closed, R-004)")

    if await store.search(
        collection=collection, vector=list(base), top_k=0,
        query_filter=_scope("ws-a"), query_identity=CONTRACT_IDENTITY,
    ):
        _fail("top_k=0 must return nothing")

    removed = await store.delete(collection=collection, ids=["a-1"])
    if removed != 1:
        _fail(f"delete by id must return the number removed, got {removed}")
    after_delete = await store.search(
        collection=collection, vector=list(base), top_k=10,
        query_filter=_scope("ws-a"), query_identity=CONTRACT_IDENTITY,
    )
    if any(hit.chunk_id == "chunk-a-1" for hit in after_delete):
        _fail("deleted id still returned by search")

    removed_by_filter = await store.delete(
        collection=collection, ids=None, query_filter=_scope("ws-a")
    )
    if removed_by_filter != 1:
        _fail(
            "delete by workspace filter must remove only that workspace "
            f"(expected 1, got {removed_by_filter})"
        )
    survivors = await store.search(
        collection=collection, vector=list(base), top_k=10,
        query_filter=_scope("ws-b"), query_identity=CONTRACT_IDENTITY,
    )
    if not survivors:
        _fail("delete by filter removed another workspace's rows")

    wrong_dimension = VectorRecord(
        id="bad",
        vector=[1.0, 0.0],
        content="wrong dimension",
        workspace_id="ws-a",
    )
    await _expect_error(
        store.upsert(collection=collection, records=[wrong_dimension]),
        "VectorStore.upsert with a wrong-dimension vector",
    )
    await _expect_error(
        store.ensure_collection(collection=collection, dimension=0),
        "VectorStore.ensure_collection with dimension=0",
    )
    # No `close()` here: the caller owns the store lifecycle (see docstring).


async def check_lexical_store_contract(store: LexicalStore) -> None:
    """index / search / filter-by-workspace / delete round-trip shape."""
    records = [
        VectorRecord(
            id="a-1",
            vector=[0.0] * CONTRACT_DIMENSION,
            content="hybrid retrieval evidence about permissions",
            workspace_id="ws-a",
            document_id="doc-a",
            document_version_id="doc-a-v1",
            chunk_id="chunk-a-1",
            page=1,
            section="Introduction",
            metadata=CONTRACT_IDENTITY.as_metadata(),
        ),
        VectorRecord(
            id="a-2",
            vector=[0.0] * CONTRACT_DIMENSION,
            content="unrelated paragraph about cooking",
            workspace_id="ws-a",
            document_id="doc-a",
            document_version_id="doc-a-v1",
            chunk_id="chunk-a-2",
            page=2,
            section="Method",
            metadata=CONTRACT_IDENTITY.as_metadata(),
        ),
        VectorRecord(
            id="b-1",
            vector=[0.0] * CONTRACT_DIMENSION,
            content="bravo secret phrase about permissions",
            workspace_id="ws-b",
            document_id="doc-b",
            document_version_id="doc-b-v1",
            chunk_id="chunk-b-1",
            page=1,
            section="Secret",
            metadata=CONTRACT_IDENTITY.as_metadata(),
        ),
    ]
    if await store.index(records=records) != 3:
        _fail("index must return the number written")

    hits = await store.search(
        query="hybrid retrieval evidence permissions", top_k=10, query_filter=_scope("ws-a")
    )
    if not hits:
        _fail("lexical search found nothing in its own workspace")
    for hit in hits:
        if hit.workspace_id != "ws-a":
            _fail(f"ACL applied after ranking: leaked {hit.workspace_id} ({hit.content!r})")
        if hit.document_id is None or hit.chunk_id is None or hit.page is None:
            _fail(f"citation provenance lost on {hit}")
        if not math.isfinite(hit.score):
            _fail(f"score must be a finite float, got {hit.score!r}")

    if await store.search(query="permissions", top_k=5, query_filter=_scope()):
        _fail("empty workspace scope must return nothing (fail closed, R-004)")
    if await store.search(query="permissions", top_k=0, query_filter=_scope("ws-a")):
        _fail("top_k=0 must return nothing")

    if await store.delete(ids=["a-1"]) != 1:
        _fail("delete by id must return the number removed")
    after_delete = await store.search(
        query="hybrid retrieval", top_k=10, query_filter=_scope("ws-a")
    )
    if any(hit.chunk_id == "chunk-a-1" for hit in after_delete):
        _fail("deleted id still returned by lexical search")

    removed = await store.delete(ids=None, query_filter=_scope("ws-a"))
    if removed != 1:
        _fail(f"delete by filter removed {removed} rows, expected only ws-a")
    survivors = await store.search(
        query="permissions", top_k=10, query_filter=_scope("ws-b")
    )
    if not survivors:
        _fail("delete by filter removed another workspace's rows")


async def check_embedding_provider_contract(provider: EmbeddingProvider) -> None:
    """Call shape + dimension/revision passthrough (no weights downloaded)."""
    if not provider.model_name:
        _fail("EmbeddingProvider.model_name must be set (routing contract)")
    if not provider.revision:
        _fail("EmbeddingProvider.revision must be set (version pin contract, PD-002)")
    if provider.dimension <= 0:
        _fail(f"EmbeddingProvider.dimension must be > 0, got {provider.dimension}")

    vectors = await provider.embed_documents(["alpha", "beta"])
    if len(vectors) != 2:
        _fail(f"embed_documents must return one vector per input, got {len(vectors)}")
    for vector in vectors:
        if len(vector) != provider.dimension:
            _fail(f"dimension mismatch: expected {provider.dimension}, got {len(vector)}")
        if any(not math.isfinite(value) for value in vector):
            _fail("embeddings must be finite floats")
        # Q1: a zero vector carries no semantic signal and would match
        # everything under cosine similarity — a manufactured embedding.
        if all(math.isclose(v, 0.0, abs_tol=1e-9) for v in vector):
            _fail(f"embedding provider must not return a zero vector: {vector}")

    again = await provider.embed_documents(["alpha", "beta"])
    if again != vectors:
        _fail("embedding must be deterministic for identical input")

    query_vector = await provider.embed_query("alpha")
    if len(query_vector) != provider.dimension:
        _fail(f"query vector dimension mismatch: {len(query_vector)} != {provider.dimension}")

    # Q1: the query vector must also be non-zero.
    if all(math.isclose(v, 0.0, abs_tol=1e-9) for v in query_vector):
        _fail("embedding provider must not return a zero query vector")

    if await provider.embed_documents([]) != []:
        _fail("empty batch must return an empty list (no crash, no network call)")

    await _expect_error(
        provider.embed_documents([123]),  # type: ignore[list-item]
        "EmbeddingProvider.embed_documents with a non-string item",
    )


async def check_reranker_contract(
    reranker: Reranker,
    *,
    query: str,
    documents: Sequence[RetrievedItem],
    top_k: int | None = 3,
) -> None:
    """Call shape: re-scored subset, provenance preserved, input untouched."""
    if not reranker.model_name:
        _fail("Reranker.model_name must be set (routing contract, PD-002)")
    if not documents:
        _fail("caller must pass at least one document to rerank")

    before = [document.model_dump() for document in documents]
    ranked = await reranker.rerank(query=query, documents=documents, top_k=top_k)
    _require_instance(ranked, list, "rerank return value")
    expected_count = len(documents) if top_k is None else min(top_k, len(documents))
    if len(ranked) != expected_count:
        _fail(f"top_k={top_k} ignored: expected {expected_count} items, got {len(ranked)}")
    if not ranked:
        _fail("rerank returned an empty list for non-empty input")

    source_keys = {(document.document_id, document.chunk_id) for document in documents}
    previous = math.inf
    for item in ranked:
        if (item.document_id, item.chunk_id) not in source_keys:
            _fail(f"rerank invented a document: {item}")
        if item.score > previous + 1e-9:
            _fail(f"rerank results must be sorted by descending score: {ranked}")
        previous = item.score
        if item.content == "":
            _fail(f"rerank lost the evidence text: {item}")

    after = [document.model_dump() for document in documents]
    if after != before:
        _fail("rerank must not mutate its input documents")

    empty = await reranker.rerank(query=query, documents=[], top_k=top_k)
    if empty != []:
        _fail("rerank of an empty list must return an empty list")


def check_rrf_determinism(fuse: Callable[[Sequence[Sequence[str]]], Mapping[str, float]]) -> None:
    """RRF fusion must be pinned, repeatable and independent of input order.

    `fuse` may be any implementation (the M0 harness runs its own reference, M3
    runs the product one); the numbers below come straight from the RRF
    definition score(d) = sum 1/(k + rank).
    """
    k = 60
    rankings: list[list[str]] = [
        ["d1", "d2", "d3"],
        ["d3", "d1"],
        ["d2"],
    ]
    expected = {
        "d1": math.fsum([1 / (k + 1), 1 / (k + 2)]),
        "d2": math.fsum([1 / (k + 2), 1 / (k + 1)]),  # rank 2 in list 1, rank 1 in list 3
        "d3": math.fsum([1 / (k + 3), 1 / (k + 1)]),
    }

    fused = dict(fuse(rankings))
    for doc_id, value in expected.items():
        if doc_id not in fused:
            _fail(f"fusion dropped {doc_id}: {fused}")
        if not math.isclose(fused[doc_id], value, rel_tol=1e-12, abs_tol=1e-12):
            _fail(f"fusion score for {doc_id} = {fused[doc_id]!r}, expected {value!r}")

    for _ in range(5):
        if dict(fuse(rankings)) != fused:
            _fail("fusion is not deterministic across repeated calls")

    permuted = dict(fuse(list(reversed(rankings))))
    if set(permuted) != set(fused):
        _fail(f"reordering rankings changed the document set: {permuted} vs {fused}")
    for doc_id, value in fused.items():
        if not math.isclose(permuted[doc_id], value, rel_tol=1e-12, abs_tol=1e-12):
            _fail(
                f"fusion depends on ranking order: {doc_id} {permuted[doc_id]} != {value}"
            )

    if dict(fuse([])) != {}:
        _fail("fusion of no rankings must return an empty mapping")


def check_retrieval_results_contract(
    items: Sequence[RetrievedItem],
    *,
    acl_scope_workspaces: Sequence[str],
    top_k: int,
) -> None:
    """Final pipeline output: in-scope only, bounded, citation-ready."""
    if len(items) > top_k:
        _fail(f"context selection ignored top_k={top_k}: {len(items)} items")
    allowed = set(acl_scope_workspaces)
    for item in items:
        if item.workspace_id is None:
            _fail(f"item carries no workspace_id (ACL unattributable): {item}")
        if item.workspace_id not in allowed:
            _fail(f"cross-workspace leak in final context: {item.workspace_id} not in {allowed}")
        if item.document_id is None or item.content == "":
            _fail(f"item is not citation-ready: {item}")
        if not math.isfinite(item.score):
            _fail(f"item score is not finite: {item.score!r}")
