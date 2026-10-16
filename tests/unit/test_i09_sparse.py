import asyncio

from retrieval.adapters.qdrant_store import (
    QdrantVectorStore,
    VectorStoreMissingSparse,
)


class _Vec:
    def __init__(self, size):
        self.size = size


class _Sparse:
    pass


class _Info:
    def __init__(self, size, sparse_ok):
        self.config = type("cfg", (), {})()
        self.config.params = type("params", (), {})()
        self.config.params.vectors = {"dense": _Vec(size)}
        self.config.params.sparse_vectors = {"sparse": _Sparse()} if sparse_ok else {}


class _Client:
    def __init__(self, sparse_ok):
        self.sparse_ok = sparse_ok

    async def collection_exists(self, name):
        return True

    async def get_collection(self, name):
        return _Info(1024, self.sparse_ok)

    async def close(self):
        pass


def _run(sparse_ok):
    store = QdrantVectorStore(client=_Client(sparse_ok))
    async def go():
        try:
            await store.ensure_collection(collection="coll", dimension=1024)
        except VectorStoreMissingSparse as e:
            assert e.collection == "coll"
            return False
        return True
    return asyncio.run(go())


def test_missing_sparse_loud():
    assert _run(False) is False


def test_fresh_sparse_declared():
    # fresh collection create path includes sparse param by code inspection
    import inspect

    from retrieval.adapters.qdrant_store import QdrantVectorStore
    src = inspect.getsource(QdrantVectorStore.ensure_collection)
    assert "SparseVectorParams" in src
