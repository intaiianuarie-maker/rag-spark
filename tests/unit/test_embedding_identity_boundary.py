"""T-R06b — identity is VERIFIED at the collection boundary, not just labelled.

dev2's T-R06 built and tested ``retrieval.embedding_identity`` and deferred the
wiring to this card. Two call sites, both in
``retrieval/adapters/qdrant_store.py``:

* **write** — ``validate_records`` refuses a record with no identity, and
  refuses one whose identity disagrees with the collection's (all four fields or
  nothing), naming the disagreeing key. An unverified batch means ZERO writes,
  which is the property the vector checks already had.
* **query** — ``search`` resolves the collection's identity (write cache, else a
  read-only single-point probe) and refuses an UNKNOWN collection or an
  incompatible query provider BEFORE any retrieval is issued.

Nothing here reimplements dev2's comparison: the tests import the same
``verify_record_identity`` / ``verify_query_identity`` / ``audit_identity`` the
store calls, and the e2e audit uses ``audit_identity`` over the records an
ingestion actually produced.

Scratch collections only; ``knowledge`` is never named.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from retrieval.adapters.qdrant_store import (
    QdrantVectorStore,
    VectorStoreError,
    VectorStoreIdentityError,
)
from retrieval.embedding_identity import (
    IDENTITY_KEYS,
    EmbeddingIdentity,
    EmbeddingIdentityError,
    audit_identity,
    identity_from_metadata,
    merge_identity,
    observed_dimension,
    provider_identity,
    verify_query_identity,
    verify_record_identity,
)
from retrieval.interfaces import SearchFilter, VectorRecord

pytestmark = [pytest.mark.unit]

DIM = 4
#: Scratch names. The live index is never named in this suite.
COLL = "knowledge_test_r06b"
OTHER = "knowledge_test_r06b_other"

BASE = EmbeddingIdentity(
    provider="GemmaStub", model="google/gemini-embedding-2", revision="rev-gemini", dimension=DIM
)
#: Same model, different pinned revision: not comparable (dev2's rule).
OTHER_REV = EmbeddingIdentity(
    provider="GemmaStub", model="google/gemini-embedding-2", revision="rev-gemini-2", dimension=DIM
)
#: Same model at another width: also not comparable.
OTHER_DIM = EmbeddingIdentity(
    provider="GemmaStub", model="google/gemini-embedding-2", revision="rev-gemini", dimension=DIM * 2
)
UNKNOWN_MODEL = EmbeddingIdentity(
    provider="OtherProvider", model="text-embed-3", revision="rev-1", dimension=DIM
)


class StubQdrant:
    """Alias/collection/payload bookkeeping — records every mutating call."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        self.deleted: list[str] = []
        self.search_calls: list[str] = []
        self.upsert_calls = 0

    def seed(self, name: str, *, points: int = 2, dimension: int = DIM) -> None:
        self.collections[name] = {
            "points": {
                f"p{i}": {"payload": {"metadata": BASE.as_metadata()}} for i in range(points)
            },
            "dimension": dimension,
        }

    def _target(self, name: str) -> str:
        return self.aliases.get(name, name)

    async def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.collections or collection_name in self.aliases

    async def get_collection(self, collection_name: str) -> Any:
        info = self.collections[self._target(collection_name)]
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": SimpleNamespace(size=info["dimension"])}
                )
            ),
            points_count=len(info["points"]),
        )

    async def create_collection(self, *, collection_name: str, **kwargs: Any) -> None:
        self.collections[collection_name] = {"points": {}, "dimension": DIM}

    async def delete_collection(self, collection_name: str) -> bool:
        self.deleted.append(collection_name)
        self.collections.pop(collection_name, None)
        return True

    async def upsert(self, *, collection_name: str, points: list[Any], **kwargs: Any) -> None:
        self.upsert_calls += 1
        stored = self.collections.setdefault(
            collection_name, {"points": {}, "dimension": DIM}
        )
        for point in points:
            stored["points"][str(point.id)] = {"payload": dict(point.payload)}

    async def scroll(self, *, collection_name: str, limit: int = 1, **kwargs: Any) -> Any:
        target = self._target(collection_name)
        info = self.collections.get(target)
        if not info:
            return ([], None)
        points = [
            SimpleNamespace(id=pid, payload=entry["payload"])
            for pid, entry in list(info["points"].items())[:limit]
        ]
        # REAL qdrant-client contract: scroll returns the PAIR
        # (points, next_page_offset). The old stub returned
        # SimpleNamespace(points=...), which let _collection_identity_of
        # resolve every collection to None and refuse every query (T-R07).
        return (points, None)

    async def query_points(self, *, collection_name: str, **kwargs: Any) -> Any:
        # Reached only AFTER the identity gate passed.
        self.search_calls.append(collection_name)
        return SimpleNamespace(points=[])

    async def get_aliases(self) -> Any:
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=alias, collection_name=target)
                for alias, target in self.aliases.items()
            ]
        )

    async def update_collection_aliases(self, *, change_aliases_operations: list) -> None:
        for operation in change_aliases_operations:
            if hasattr(operation, "delete_alias"):
                self.aliases.pop(operation.delete_alias.alias_name, None)
            else:
                create = operation.create_alias
                self.aliases[create.alias_name] = create.collection_name

    async def close(self) -> None:
        return None


def make_store(client: StubQdrant) -> QdrantVectorStore:
    return QdrantVectorStore(collection=COLL, client=client)  # type: ignore[arg-type]


def record(
    rid: str = "chunk-1",
    *,
    metadata: dict[str, Any] | None = None,
    workspace_id: str = "ws-a",
) -> VectorRecord:
    return VectorRecord(
        id=rid,
        vector=[0.5] * DIM,
        content="evidence about permissions",
        workspace_id=workspace_id,
        document_id="doc-a",
        chunk_id=rid,
        metadata=BASE.as_metadata() if metadata is None else metadata,
    )


def scope(*workspaces: str) -> SearchFilter:
    return SearchFilter(workspace_ids=list(workspaces or ("ws-a",)))


# --------------------------------------------------------------------------- #
# Call site 1 — validate_records: unlabelled and incompatible refused
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_unlabelled_record_is_refused_with_the_missing_keys_named() -> None:
    client = StubQdrant()
    client.seed(COLL, points=0)
    store = make_store(client)

    with pytest.raises(VectorStoreIdentityError) as exc:
        await store.upsert(collection=COLL, records=[record("a-1", metadata={"language": "en"})])

    assert exc.value.reason == "unlabelled_record"
    for key in IDENTITY_KEYS:
        assert key in str(exc.value), key
    assert client.upsert_calls == 0, "an unlabelled batch must write NOTHING"
    assert exc.value.record_id == "a-1"


@pytest.mark.unit
async def test_partially_labelled_record_is_still_unlabelled() -> None:
    client = StubQdrant()
    client.seed(COLL, points=0)
    store = make_store(client)
    partial = dict(BASE.as_metadata())
    partial.pop("embedding_revision")

    with pytest.raises(VectorStoreIdentityError) as exc:
        await store.upsert(collection=COLL, records=[record("b-1", metadata=partial)])

    assert "embedding_revision" in str(exc.value)
    assert client.upsert_calls == 0


@pytest.mark.unit
async def test_incompatible_record_is_refused_with_the_per_field_reason() -> None:
    """Each of the four keys can refuse a write, and the key is named."""
    for field, wrong in (
        ("embedding_provider", UNKNOWN_MODEL),
        (
            "embedding_model",
            EmbeddingIdentity(
                provider=BASE.provider, model="text-embed-3", revision=BASE.revision, dimension=DIM
            ),
        ),
        ("embedding_revision", OTHER_REV),
        ("embedding_dimension", OTHER_DIM),
    ):
        client = StubQdrant()
        client.seed(COLL, points=0)
        store = make_store(client)
        # The collection was written by BASE; a record claiming `wrong` must not
        # be admitted to it.
        await store.upsert(collection=COLL, records=[record("seed")])
        before = client.upsert_calls

        with pytest.raises(VectorStoreIdentityError) as exc:
            await store.upsert(
                collection=COLL, records=[record("x-1", metadata=wrong.as_metadata())]
            )

        assert exc.value.reason == "incompatible_record"
        assert exc.value.field == field, f"{field} must be named"
        assert client.upsert_calls == before, "a refused batch writes NOTHING"


@pytest.mark.unit
async def test_one_unverified_record_blocks_the_whole_batch() -> None:
    client = StubQdrant()
    client.seed(COLL, points=0)
    store = make_store(client)

    with pytest.raises(VectorStoreIdentityError):
        await store.upsert(
            collection=COLL,
            records=[record("good-1"), record("bad-2", metadata={"language": "en"})],
        )
    assert client.upsert_calls == 0


@pytest.mark.unit
async def test_verified_identity_is_cached_for_the_collection() -> None:
    client = StubQdrant()
    client.seed(COLL, points=0)
    store = make_store(client)
    await store.upsert(collection=COLL, records=[record("a-1")])
    assert store._collection_identity[COLL] == BASE


# --------------------------------------------------------------------------- #
# Call site 2 — search: unknown / incompatible collections refuse BEFORE retrieval
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_unknown_collection_identity_is_refused_and_never_assumed() -> None:
    """A collection with no recorded identity must not be searched."""
    client = StubQdrant()
    client.seed(COLL, points=2)
    # Strip the identity the seeder stamped: this is the legacy/unlabelled shape.
    for entry in client.collections[COLL]["points"].values():
        entry["payload"] = {"metadata": {"language": "en"}}
    store = make_store(client)

    with pytest.raises(VectorStoreIdentityError) as exc:
        await store.search(
            collection=COLL, vector=[0.5] * DIM, top_k=3, query_filter=scope()
        )

    assert exc.value.reason == "unknown_collection"
    assert "unverified" in str(exc.value)
    assert client.search_calls == [], "no retrieval may be issued against an unknown index"


@pytest.mark.unit
async def test_empty_collection_is_refused_as_unknown() -> None:
    client = StubQdrant()
    client.seed(COLL, points=0)
    store = make_store(client)
    with pytest.raises(VectorStoreIdentityError) as exc:
        await store.search(collection=COLL, vector=[0.5] * DIM, top_k=2, query_filter=scope())
    assert exc.value.reason == "unknown_collection"
    assert client.search_calls == []


@pytest.mark.unit
async def test_incompatible_query_provider_is_refused_before_retrieval() -> None:
    client = StubQdrant()
    client.seed(COLL, points=2)
    store = make_store(client)
    await store.upsert(collection=COLL, records=[record("a-1")])

    with pytest.raises(VectorStoreIdentityError) as exc:
        await store.search(
            collection=COLL,
            vector=[0.5] * DIM,
            top_k=2,
            query_filter=scope(),
            query_identity=OTHER_REV,
        )

    assert exc.value.reason == "incompatible_query"
    assert "embedding_revision" in str(exc.value)
    assert client.search_calls == [], "refusal happens before the retrieval"


@pytest.mark.unit
async def test_compatible_query_passes_and_probes_read_only() -> None:
    client = StubQdrant()
    client.seed(COLL, points=2)
    store = make_store(client)
    # No write first: the identity must come from the read-only probe.
    assert await store.search(
        collection=COLL, vector=[0.5] * DIM, top_k=2, query_filter=scope(),
        query_identity=BASE,
    ) == []
    assert client.search_calls == [COLL]
    assert store._collection_identity[COLL] == BASE


@pytest.mark.unit
async def test_search_shortcuts_still_apply_before_the_identity_gate() -> None:
    """top_k/ACL short-circuits keep their behaviour (no probe, no refusal)."""
    client = StubQdrant()
    client.seed(COLL, points=1)
    store = make_store(client)
    assert (
        await store.search(
            collection=COLL, vector=[0.5] * DIM, top_k=0, query_filter=scope()
        )
        == []
    )
    empty = SearchFilter(workspace_ids=[])
    assert await store.search(
        collection=COLL, vector=[0.5] * DIM, top_k=3, query_filter=empty
    ) == []
    assert client.search_calls == []


@pytest.mark.unit
async def test_store_imports_dev2_verifiers_rather_than_reimplementing() -> None:
    """The gate is dev2's code, wired — not a second comparison written here."""
    import inspect

    import retrieval.adapters.qdrant_store as qdrant_store

    source = inspect.getsource(qdrant_store)
    assert "verify_record_identity(" in source
    assert "verify_query_identity(" in source
    # No local re-derivation of the four-key comparison.
    assert "as_tuple() ==" not in source
    boundary = source.split("def validate_records")[1].split("def _to_point_id")[0]
    # ``mismatch_reason`` may appear only as dev2's own reason text passthrough.
    without_passthrough = boundary.replace(
        "identity_from_metadata(metadata).mismatch_reason(identity)", ""
    )
    assert "mismatch_reason" not in without_passthrough


# --------------------------------------------------------------------------- #
# e2e: normal ingestion into a rebuilt scratch collection passes a FULL audit
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_e2e_ingestion_into_a_rebuilt_collection_passes_a_full_identity_audit() -> None:
    """Rebuild → promote → ingest → audit, on scratch collections only.

    The audit is dev2's ``audit_identity`` over the payloads the store actually
    wrote, so a pass means the records on the index really are the identity
    they claim — not that the caller believed so.
    """
    client = StubQdrant()
    replacement = f"{COLL}_v2"
    client.seed(replacement, points=0)
    store = make_store(client)

    # --- 1. normal ingestion: identity stamped by the same helpers the
    # production write paths use (provider_identity + merge_identity).
    class GemmaStub:
        model_name = "google/gemini-embedding-2"
        revision = "rev-gemini"

    vectors = [[0.4, 0.3, 0.2, 0.1] for _ in range(4)]
    identity = provider_identity(GemmaStub(), observed_dimension(vectors))
    assert identity == BASE  # the stub provider matches BASE on purpose
    batch = [
        record(
            f"chunk-{index}",
            metadata=merge_identity({"language": "en"}, identity),
            workspace_id="ws-a" if index % 2 else "ws-b",
        )
        for index in range(4)
    ]
    assert await store.upsert(collection=replacement, records=batch) == 4

    # --- 2. promote the rebuilt collection onto the serving alias (R05 path).
    alias = f"{COLL}__serving"
    client.seed(COLL, points=0)
    await store.ensure_serving_alias(physical=COLL, alias=alias)
    result = await store.promote_replacement(
        live=alias, replacement=replacement, expected_points=4
    )
    assert result.new_target == replacement
    assert client.deleted == []

    # --- 3. FULL identity audit of what is on the index, read back.
    payloads = [
        entry["payload"]
        for entry in client.collections[replacement]["points"].values()
    ]
    audit = audit_identity([payload.get("metadata") or {} for payload in payloads], identity)
    assert audit == {
        "total": 4,
        "labelled": 4,
        "unlabelled": 0,
        "incompatible": 0,
        "mismatch_reasons": {},
        "requested": identity.as_metadata(),
    }

    # --- 4. every payload individually verifies against the same identity.
    for payload in payloads:
        verify_record_identity(payload.get("metadata"), identity, collection=replacement)

    # --- 5. the serving alias resolves, is readable, and answers a compatible query.
    serving = await store.verify_serving_readable(alias)
    assert serving["readable"] is True and serving["resolves_to"] == replacement
    await store.search(
        collection=alias,
        vector=[0.4, 0.3, 0.2, 0.1],
        top_k=2,
        query_filter=scope(),
        query_identity=identity,
    )
    assert client.search_calls == [alias]

    # --- 6. and the audit refuses a foreign provider on the same records.
    foreign = audit_identity(
        [payload.get("metadata") or {} for payload in payloads], UNKNOWN_MODEL
    )
    assert foreign["incompatible"] == 4
    # dev2's audit names the FIRST disagreeing field per record (provider here),
    # so the reason histogram is one bucket, not two.
    assert set(foreign["mismatch_reasons"]) == {"embedding_provider"}
    assert foreign["labelled"] == 4 and foreign["unlabelled"] == 0
    with pytest.raises(EmbeddingIdentityError):
        verify_query_identity(identity, UNKNOWN_MODEL, collection=alias)


@pytest.mark.unit
def test_this_suite_names_no_live_collection() -> None:
    from retrieval.serving import AMBIENT_DEFAULT_COLLECTION

    assert AMBIENT_DEFAULT_COLLECTION not in (COLL, OTHER, OTHER_DIM.as_tuple()[0])
    assert COLL.startswith("knowledge_test_r06b")
    assert identity_from_metadata(BASE.as_metadata()) == BASE
    assert isinstance(VectorStoreError("x"), VectorStoreError)