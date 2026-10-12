"""infinity embedding adapter contract (TASK-M3-002).

Runs the shared `check_embedding_provider_contract` against
`InfinityEmbeddingProvider` pointed at a **fake HTTP server** (stdlib
`http.server` on 127.0.0.1) that mimics infinity's OpenAI-compatible
`/embeddings`. No model weights, no external service, no API keys.

Also pins the adapter-specific promises: query/document prefixes, input-order
preservation, batching, empty-batch short-circuit, and typed errors on
non-200 / bad-dimension responses.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from retrieval.adapters.infinity_embed import (
    EmbeddingProviderError,
    InfinityEmbeddingProvider,
)
from retrieval.contract_tests import check_embedding_provider_contract

pytestmark = [pytest.mark.unit, pytest.mark.contract]

DIMENSION = 1024


def _vector(text: str, dimension: int) -> list[float]:
    """Deterministic vector for a text (same text -> same vector)."""
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(f"{text}:{counter}".encode()).digest()
        values.extend(byte / 255.0 for byte in digest)
        counter += 1
    return values[:dimension]


_INVALID_COMPONENTS: dict[str, Any] = {
    "nan": float("nan"),
    "pos_inf": float("inf"),
    "neg_inf": float("-inf"),
    "str_nan": "nan",
    "str_inf": "inf",
    "str_neg_inf": "-inf",
    "non_numeric": "not-a-number",
    "null": None,
}


def _vector_with_invalid(text: str, dimension: int, invalid: Any) -> list[Any]:
    """Valid deterministic vector with one invalid component at position 0."""
    vec: list[Any] = list(_vector(text, dimension))
    vec[0] = invalid
    return vec


class _Handler(BaseHTTPRequestHandler):
    server: _StubServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({"path": self.path, "body": body})

        inputs = body.get("input") or []

        if self.server.mode == "http_500":
            self._send(500, {"error": "boom"})
            return
        if self.server.mode == "non_json":
            raw = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.server.mode == "zero_vector":
            # Simulates a broken/empty model returning all-zero embeddings.
            data = [
                {"object": "embedding", "index": index, "embedding": [0.0] * DIMENSION}
                for index in range(len(inputs))
            ]
            self._send(200, {"object": "list", "model": body.get("model"), "data": data})
            return
        if self.server.mode == "zero_vector_last":
            # Valid vectors for all but the final input; the final input gets an
            # all-zero vector. Proves a single zero vector fails the *whole*
            # batch and that preceding valid vectors are not returned (no partial
            # success) — Q1 req #3 ("fail the whole batch").
            last = len(inputs) - 1
            data = [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": (
                        [0.0] * DIMENSION
                        if index == last
                        else _vector(text, DIMENSION)
                    ),
                }
                for index, text in enumerate(inputs)
            ]
            self._send(200, {"object": "list", "model": body.get("model"), "data": data})
            return
        if self.server.mode.startswith("invalid_component:"):
            # Small parameterized invalid-component mode; the invalid component
            # sits in the final vector after a valid vector so whole-call
            # failure (no partial return) is exercised.
            key = self.server.mode.split(":", 1)[1]
            invalid = _INVALID_COMPONENTS[key]
            last = len(inputs) - 1
            data = [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": (
                        _vector_with_invalid(text, DIMENSION, invalid)
                        if index == last
                        else _vector(text, DIMENSION)
                    ),
                }
                for index, text in enumerate(inputs)
            ]
            self._send(200, {"object": "list", "model": body.get("model"), "data": data})
            return
        if self.server.mode == "nonfinite_truncated_tail":
            # Non-finite component in a tail that dimension adaptation would
            # otherwise truncate: still rejected (finiteness checked first).
            last = len(inputs) - 1
            data = [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": (
                        (_vector(text, DIMENSION) + [float("nan")] * 64)
                        if index == last
                        else _vector(text, DIMENSION)
                    ),
                }
                for index, text in enumerate(inputs)
            ]
            self._send(200, {"object": "list", "model": body.get("model"), "data": data})
            return
        if self.server.mode == "near_zero":
            data = [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": [1e-12] * DIMENSION,
                }
                for index in range(len(inputs))
            ]
            self._send(200, {"object": "list", "model": body.get("model"), "data": data})
            return
        if self.server.mode == "large_finite":
            data = [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": [1e308] * DIMENSION,
                }
                for index in range(len(inputs))
            ]
            self._send(200, {"object": "list", "model": body.get("model"), "data": data})
            return

        dimension = 3 if self.server.mode == "bad_dim" else DIMENSION
        data = [
            {"object": "embedding", "index": index, "embedding": _vector(text, dimension)}
            for index, text in enumerate(inputs)
        ]
        if self.server.reverse_order:
            data = list(reversed(data))
        self._send(200, {"object": "list", "model": body.get("model"), "data": data})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        pass


class _StubServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    mode: str
    reverse_order: bool


@pytest.fixture
def stub() -> Any:
    server = _StubServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.mode = "ok"
    server.reverse_order = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
async def provider(stub: _StubServer) -> Any:
    host, port = stub.server_address
    client = httpx.AsyncClient(timeout=5.0)
    adapter = InfinityEmbeddingProvider(base_url=f"http://{host}:{port}", client=client)
    yield adapter
    # The fixture owns the injected client; adapter.aclose() must not touch it.
    await client.aclose()


# --- shared contract --------------------------------------------------------


async def test_adapter_satisfies_embedding_contract(provider: InfinityEmbeddingProvider) -> None:
    """Call shape, dimension (1024), determinism, empty batch, invalid input."""
    await check_embedding_provider_contract(provider)


async def test_model_identity_is_bge_m3_1024(provider: InfinityEmbeddingProvider) -> None:
    assert provider.model_name == "BAAI/bge-m3"
    assert provider.dimension == 1024
    assert provider.revision  # version-pin contract (PD-002)


async def test_request_uses_openai_compatible_payload(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    await provider.embed_documents(["alpha", "beta"])
    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request["path"] == "/embeddings"
    assert request["body"]["model"] == "BAAI/bge-m3"
    assert request["body"]["input"] == ["alpha", "beta"]
    assert request["body"]["encoding_format"] == "float"


# --- prefixes ---------------------------------------------------------------


async def test_query_and_document_prefixes_are_applied(stub: _StubServer) -> None:
    host, port = stub.server_address
    adapter = InfinityEmbeddingProvider(
        base_url=f"http://{host}:{port}",
        query_prefix="query: ",
        document_prefix="passage: ",
    )
    try:
        await adapter.embed_query("salut")
        await adapter.embed_documents(["unul", "doi"])
    finally:
        await adapter.aclose()

    sent = [text for request in stub.requests for text in request["body"]["input"]]
    assert sent == ["query: salut", "passage: unul", "passage: doi"]


async def test_bge_m3_defaults_have_no_prefix(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    await provider.embed_query("salut")
    assert stub.requests[0]["body"]["input"] == ["salut"]


# --- ordering / batching ----------------------------------------------------


async def test_input_order_is_preserved_when_server_reorders(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    stub.reverse_order = True
    vectors = await provider.embed_documents(["alpha", "beta", "gamma"])
    assert vectors == [_vector(t, DIMENSION) for t in ("alpha", "beta", "gamma")]


async def test_batching_splits_requests(stub: _StubServer) -> None:
    host, port = stub.server_address
    adapter = InfinityEmbeddingProvider(base_url=f"http://{host}:{port}", batch_size=2)
    try:
        vectors = await adapter.embed_documents(["a", "b", "c", "d", "e"])
    finally:
        await adapter.aclose()
    assert len(vectors) == 5
    assert [len(r["body"]["input"]) for r in stub.requests] == [2, 2, 1]


async def test_empty_batch_makes_no_request(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    assert await provider.embed_documents([]) == []
    assert stub.requests == []


# --- negative paths ---------------------------------------------------------


async def test_non_string_item_raises_before_network(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    with pytest.raises(ValueError):
        await provider.embed_documents([123])  # type: ignore[list-item]
    assert stub.requests == []


async def test_empty_query_raises(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    with pytest.raises(ValueError):
        await provider.embed_query("")
    assert stub.requests == []


async def test_http_error_is_typed(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    stub.mode = "http_500"
    with pytest.raises(EmbeddingProviderError):
        await provider.embed_documents(["alpha"])


async def test_non_json_body_is_typed(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    stub.mode = "non_json"
    with pytest.raises(EmbeddingProviderError):
        await provider.embed_documents(["alpha"])


async def test_wrong_dimension_is_rejected(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    stub.mode = "bad_dim"
    with pytest.raises(EmbeddingProviderError):
        await provider.embed_documents(["alpha"])


async def test_zero_vector_is_rejected(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    """Q1: infinity must never return an all-zero embedding.

    A zero vector carries no semantic signal and would match every query
    equally under cosine similarity — a manufactured vector that could
    produce false success. The adapter must reject it loudly rather than
    silently indexing garbage.
    """
    stub.mode = "zero_vector"
    with pytest.raises(EmbeddingProviderError, match="zero-vector"):
        await provider.embed_documents(["alpha"])
    with pytest.raises(EmbeddingProviderError, match="zero-vector"):
        await provider.embed_query("alpha")


async def test_zero_vector_in_batch_fails_whole_batch(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    """Q1 req #3: any zero vector fails the whole batch with no partial success.

    The server returns valid embeddings for the first inputs and an all-zero
    embedding for the last; the adapter must reject the entire batch so the
    preceding valid vectors are never returned.
    """
    stub.mode = "zero_vector_last"
    with pytest.raises(EmbeddingProviderError, match="zero-vector"):
        await provider.embed_documents(["alpha", "beta", "gamma"])
    # The whole group was sent in a single batched request; nothing leaked.
    assert len(stub.requests) == 1
    assert len(stub.requests[0]["body"]["input"]) == 3


async def test_unreachable_server_is_typed() -> None:
    # Port 1 on localhost refuses connections; no server needed.
    adapter = InfinityEmbeddingProvider(base_url="http://127.0.0.1:1", timeout=1.0)
    try:
        with pytest.raises(EmbeddingProviderError):
            await adapter.embed_documents(["alpha"])
    finally:
        await adapter.aclose()


async def test_injected_client_is_not_closed_by_aclose(stub: _StubServer) -> None:
    host, port = stub.server_address
    client = httpx.AsyncClient(timeout=5.0)
    adapter = InfinityEmbeddingProvider(base_url=f"http://{host}:{port}", client=client)
    await adapter.embed_query("alpha")
    await adapter.aclose()
    assert not client.is_closed  # caller-owned lifecycle
    await client.aclose()


# --- Q1 finite-embedding guard ------------------------------------------------


@pytest.mark.parametrize(
    "case",
    ["nan", "pos_inf", "neg_inf", "str_nan", "str_inf", "str_neg_inf", "non_numeric", "null"],
)
async def test_invalid_components_rejected_for_query_and_documents(
    provider: InfinityEmbeddingProvider, stub: _StubServer, case: str
) -> None:
    """Q1 finite-embeddings: NaN/±Inf (incl. convertible strings), non-numeric, null.

    The invalid component sits in the final vector after a valid vector, so a
    raise proves whole-call failure with no partial return — for both entry
    points. Document input has two items to test whole-call failure.
    """
    stub.mode = f"invalid_component:{case}"
    with pytest.raises(EmbeddingProviderError, match=r"non-finite|invalid numeric"):
        await provider.embed_documents(["alpha", "beta"])
    with pytest.raises(EmbeddingProviderError, match=r"non-finite|invalid numeric"):
        await provider.embed_query("alpha")
    assert len(stub.requests[0]["body"]["input"]) == 2


async def test_nonfinite_in_truncated_tail_still_rejected(
    provider: InfinityEmbeddingProvider, stub: _StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-finite tail components reject even when adaptation would truncate them.

    Finiteness is checked before dimension adaptation can discard the tail.
    """
    monkeypatch.setenv("ALLOW_DIMENSION_ADAPTATION", "true")
    stub.mode = "nonfinite_truncated_tail"
    with pytest.raises(EmbeddingProviderError, match="non-finite"):
        await provider.embed_documents(["alpha", "beta"])
    with pytest.raises(EmbeddingProviderError, match="non-finite"):
        await provider.embed_query("alpha")


async def test_near_zero_positive_vector_is_rejected(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    """1024 components of 1e-12 carry no signal: same zero-vector rejection."""
    stub.mode = "near_zero"
    with pytest.raises(EmbeddingProviderError, match="zero-vector"):
        await provider.embed_documents(["alpha"])
    with pytest.raises(EmbeddingProviderError, match="zero-vector"):
        await provider.embed_query("alpha")


async def test_finite_vectors_survive_unchanged(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    """Valid converted values pass through exactly (no clamping/rewriting)."""
    vectors = await provider.embed_documents(["alpha", "beta"])
    assert vectors == [_vector("alpha", DIMENSION), _vector("beta", DIMENSION)]
    assert await provider.embed_query("alpha") == _vector("alpha", DIMENSION)


async def test_large_finite_vector_returns_without_overflow(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    """1024 components of 1e308: finite, nonzero — no arithmetic overflow."""
    stub.mode = "large_finite"
    vectors = await provider.embed_documents(["alpha"])
    assert vectors == [[1e308] * DIMENSION]
    assert await provider.embed_query("alpha") == [1e308] * DIMENSION
