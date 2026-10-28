"""Prove reversible nested label updates on a private real Qdrant collection.

This is a fixture-only operation proof, not a production migration command.
No existing collection is read or changed; every point is synthetic.
"""

from __future__ import annotations

import copy
import uuid

import httpx
import pytest
from core.config import get_settings
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException

pytestmark = pytest.mark.integration

MODEL = "google/gemini-embedding-2"
OLD = "OpenRouterProvider"
NEW = "GeminiEmbedding2"
IDENTITY_FIELDS = ("embedding_provider", "embedding_model", "embedding_revision",
                   "embedding_dimension")


@pytest.fixture
def qdrant():
    client = QdrantClient(url=get_settings().qdrant_url, timeout=5, check_compatibility=False)
    try:
        client.get_collections()
    except (httpx.ConnectError, ResponseHandlingException):
        client.close()
        pytest.skip("Qdrant unavailable; start real Qdrant for the private nested-label fixture")
    collection = "embedding_label_test_" + uuid.uuid4().hex
    created = False
    try:
        client.create_collection(
            collection_name=collection,
            vectors_config={
                "dense": models.VectorParams(size=3072, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
        created = True
        yield client, collection
    finally:
        if created:
            client.delete_collection(collection_name=collection)
        client.close()


def test_exact_identity_selection_nested_label_update_and_audit_id_revert(qdrant):
    client, collection = qdrant

    def point(point_id, provider, model=MODEL):
        return models.PointStruct(
            id=point_id,
            vector={"dense": [0.125] * 3072,
                    "sparse": models.SparseVector(indices=[1, 7], values=[0.5, 1.5])},
            payload={"content": "synthetic fixture source", "workspace_id": "fixture-workspace",
                     "document_id": str(uuid.uuid4()), "chunk_id": str(uuid.uuid4()),
                     "metadata": {"embedding_provider": provider, "embedding_model": model,
                                  "embedding_revision": MODEL, "embedding_dimension": 3072,
                                  "untouched": {"source": "fixture evidence", "number": 7}}},
        )

    client.upsert(collection_name=collection,
                  points=[point(1, OLD), point(2, NEW), point(3, OLD, "other/encoder")], wait=True)
    before = {record.id: record for record in client.retrieve(
        collection_name=collection, ids=[1, 2, 3], with_payload=True, with_vectors=True
    )}
    expected = {"embedding_provider": OLD, "embedding_model": MODEL,
                "embedding_revision": MODEL, "embedding_dimension": 3072}
    selection = models.Filter(must=[
        models.FieldCondition(key=f"metadata.{key}", match=models.MatchValue(value=value))
        for key, value in expected.items()
    ])
    records, next_offset = client.scroll(
        collection_name=collection, scroll_filter=selection, limit=10,
        with_payload=models.PayloadSelectorInclude(
            include=[f"metadata.{key}" for key in IDENTITY_FIELDS]
        ), with_vectors=False,
    )
    assert next_offset is None
    assert len(records) == 1 and records[0].id == 1
    assert records[0].payload == {"metadata": expected}  # audit excludes source content
    audit = [{"id": record.id, "identity": copy.deepcopy(record.payload["metadata"])}
             for record in records]
    ids = [entry["id"] for entry in audit]
    client.set_payload(collection_name=collection, points=ids, key="metadata",
                       payload={"embedding_provider": NEW}, wait=True)
    after = {record.id: record for record in client.retrieve(
        collection_name=collection, ids=[1, 2, 3], with_payload=True, with_vectors=True
    )}
    assert client.count(collection_name=collection, exact=True).count == 3
    for point_id, original in before.items():
        expected_payload = copy.deepcopy(original.payload)
        if point_id in ids:
            expected_payload["metadata"]["embedding_provider"] = NEW
        assert after[point_id].payload == expected_payload
        assert after[point_id].vector == original.vector
        assert after[point_id].id == original.id

    # Revert only the audited IDs, never a broad reverse filter that would
    # relabel the already-canonical point 2 or the unrelated encoder point 3.
    for entry in audit:
        client.set_payload(collection_name=collection, points=[entry["id"]], key="metadata",
                           payload={"embedding_provider": entry["identity"]["embedding_provider"]},
                           wait=True)
    restored = {record.id: record for record in client.retrieve(
        collection_name=collection, ids=[1, 2, 3], with_payload=True, with_vectors=True
    )}
    for point_id, original in before.items():
        assert restored[point_id].payload == original.payload
        assert restored[point_id].vector == original.vector
