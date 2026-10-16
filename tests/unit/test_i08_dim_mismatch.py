import asyncio

from retrieval.adapters.qdrant_store import (
    QdrantVectorStore,
    VectorStoreDimensionMismatch,
)


class _Vec:
    def __init__(self, size):
        self.size = size


class _Info:
    def __init__(self, size):
        self.config = type("cfg", (), {})()
        self.config.params = type("params", (), {})()
        self.config.params.vectors = {"dense": _Vec(size)}


class _Client:
    async def collection_exists(self, name):
        return True

    async def get_collection(self, name):
        return _Info(1024)

    async def close(self):
        pass


def test_dim_mismatch_loud_typed():
    store = QdrantVectorStore(client=_Client())
    async def go():
        try:
            await store.ensure_collection(collection="coll", dimension=3072)
        except VectorStoreDimensionMismatch as e:
            assert e.expected == 3072
            assert e.actual == 1024
            assert e.collection == "coll"
            return True
        return False
    assert asyncio.run(go()) is True
