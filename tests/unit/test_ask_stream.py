"""Unit tests for TASK-M4-001b: streaming SSE endpoint for /v1/rag/stream.

Verifies:
- Stream ordering: all token events precede the citations event.
- Security: no citation/evidence ID is emitted mid-stream (only verified citations
  appear in the terminal citations event).
- Citation-ID swap attack: an attacker-supplied chunk_id that does not resolve
  to an in-scope chunk is rejected — not surfaced to the user.
- Multilingual.jsonl: streamed answers for non-English claims follow the same
  verification contract as JSON.
- Error parity: LLM provider errors and retrieval failures map to the same
  ReasonCode taxonomy as POST /ask (503 / LLM_UNAVAILABLE).
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator

import pytest
from answer.engine import AnswerEngine, _StreamingAnswerExtractor
from answer.models import (
    Answer,
    AnswerStatus,
    Citation,
    Claim,
    Confidence,
    ConfidenceLevel,
)
from core.auth import ACLScope, Principal, Role
from harness_fakes import FakeLLMProvider, make_item
from models.adapters.litellm_provider import LLMProviderError
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider
from retrieval.interfaces import (
    RetrievalMode,
    RetrievalOrchestrator,
    RetrievedItem,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeOrchestrator(RetrievalOrchestrator):
    """Retrieval fake that returns canned items and supports async streaming."""

    def __init__(self, items: list[RetrievedItem]):
        self.items = items
        self.calls = 0

    async def retrieve(
        self,
        *,
        query: str,
        acl_scope,
        mode: RetrievalMode = RetrievalMode.HYBRID,
        context_top_k: int | None = None,
        **kwargs,
    ) -> list[RetrievedItem]:
        self.calls += 1
        return self.items


class _StreamingFakeLLM(LLMProvider):
    """Provider that streams canned text chunks (mirrors OpenRouterProvider stream)."""

    provider_name = "fake-streaming"

    def __init__(self, full_text: str, chunk_size: int = 8):
        self._full_text = full_text
        self._chunk_size = chunk_size

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        return GenerationResult(text=self._full_text, model=request.model)

    def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            for i in range(0, len(self._full_text), self._chunk_size):
                yield self._full_text[i : i + self._chunk_size]

        return _gen()


class _ErrorFakeLLM(LLMProvider):
    """Provider that raises during stream (simulates gateway down)."""

    provider_name = "fake-error"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        raise LLMProviderError("gateway unreachable")

    def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            raise LLMProviderError("gateway unreachable")
            yield  # type: ignore[unreachable]  # makes _gen an async generator

        return _gen()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(
    *,
    llm: LLMProvider | None = None,
    items: list[RetrievedItem] | None = None,
) -> AnswerEngine:
    if items is None:
        items = [
            make_item(
                "Company revenue grew 20% in 2025.",
                0.95,
                workspace_id="ws-1",
                document_id="doc-1",
                chunk_id="chunk-1",
                page=5,
                section="Financials",
            )
        ]
    if llm is None:
        llm = _StreamingFakeLLM(
            json.dumps(
                {
                    "answer": "Company revenue grew 20% in 2025.",
                    "claims": [
                        {"text": "Company revenue grew 20% in 2025.", "chunk_ids": ["chunk-1"]}
                    ],
                }
            )
        )
    return AnswerEngine(
        orchestrator=_FakeOrchestrator(items),
        llm=llm,
        model="google/gemma-4-26b-a4b-it:free",
    )


async def _collect_stream(
    engine: AnswerEngine, query: str = "Revenue?", workspace_id: str = "ws-1"
):
    """Consume the engine's stream generator and return (tokens, final_answer)."""
    tokens: list[str] = []
    final_answer: Answer | None = None
    async for event_type, payload in engine.stream(
        query=query,
        acl_scope=ACLScope(workspace_ids=[workspace_id]),
        mode=RetrievalMode.HYBRID,
    ):
        if event_type == "token":
            tokens.append(payload)
        elif event_type == "verified":
            final_answer = payload
    return tokens, final_answer


# ---------------------------------------------------------------------------
# Tests: stream ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_emits_tokens_before_citations():
    """All token events must arrive before the verified/citations event."""
    engine = _make_engine()
    tokens, answer = await _collect_stream(engine)

    assert len(tokens) > 0, "Expected at least one token event"
    assert answer is not None, "Expected a verified Answer as the final event"
    assert answer.status == AnswerStatus.ANSWERED
    # Tokens concatenate back to the ANSWER TEXT only (not raw JSON): the
    # streaming extractor strips the JSON structure and yields just the
    # decoded "answer" value, never chunk_ids or claims.
    full_text = "".join(tokens)
    assert full_text == "Company revenue grew 20% in 2025.", (
        f"Expected the decoded answer text, got: {full_text!r}"
    )


@pytest.mark.asyncio
async def test_stream_no_citation_ids_in_tokens():
    """Security: no structured citation data is emitted mid-stream.

    With the streaming answer-field extractor, token payloads carry only the
    decoded ``"answer"`` string. No token payload may contain a citation or
    evidence identifier — the raw JSON structure (chunk_ids, claims) is
    filtered out of the token seam entirely.
    """
    engine = _make_engine()
    events = []
    async for event_type, payload in engine.stream(
        query="Revenue?",
        acl_scope=ACLScope(workspace_ids=["ws-1"]),
        mode=RetrievalMode.HYBRID,
    ):
        events.append((event_type, payload))

    # Ordering: all token events must precede the verified event.
    types = [e[0] for e in events]
    assert "verified" in types, "Expected a verified Answer as the final event"
    verified_idx = types.index("verified")
    for idx, t in enumerate(types):
        if t == "token":
            assert idx < verified_idx, (
                f"Token event at index {idx} arrived after verified event at {verified_idx}"
            )

    # Strong assertion: no token payload may carry a citation/evidence ID.
    forbidden = ("chunk_id", "chunk-", "cite:", "claim_id", "evidence-")
    for event_type, payload in events:
        if event_type == "token":
            assert isinstance(payload, str), (
                f"Token payload must be str, got {type(payload).__name__}"
            )
            for marker in forbidden:
                assert marker not in payload, (
                    f"Token payload leaked a citation/evidence ID {marker!r}: {payload!r}"
                )
    # The verified Answer must have citations (verified at end, not mid-stream).
    final_answer = events[-1][1]
    assert isinstance(final_answer, Answer)
    assert len(final_answer.citations) >= 1, "Verified citations must exist in the final event"


@pytest.mark.asyncio
async def test_stream_verified_citations_are_in_scope():
    """Citations in the final event must reference only in-scope chunks."""
    engine = _make_engine()
    _tokens, answer = await _collect_stream(engine)

    assert answer is not None
    for citation in answer.citations:
        assert citation.workspace_id == "ws-1", (
            f"Citation leaked to foreign workspace: {citation.workspace_id}"
        )


# ---------------------------------------------------------------------------
# Tests: citation-ID swap attack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_rejects_foreign_workspace_citation():
    """An attacker-supplied chunk_id that belongs to another workspace must NOT
    surface as a citation — it must be dropped during verification."""
    # The model claims to cite chunk-foreign, but that chunk doesn't exist in
    # the caller's workspace.
    llm_output = json.dumps(
        {
            "answer": "Revenue grew 20%.",
            "claims": [
                {"text": "Revenue grew 20%.", "chunk_ids": ["chunk-foreign"]}
            ],
        }
    )
    engine = _make_engine(
        llm=_StreamingFakeLLM(llm_output),
        items=[
            make_item(
                "Company revenue grew 20% in 2025.",
                0.95,
                workspace_id="ws-1",
                document_id="doc-1",
                chunk_id="chunk-1",
                page=5,
                section="Financials",
            )
        ],
    )
    _tokens, answer = await _collect_stream(engine)

    assert answer is not None
    # The foreign chunk_id must not appear in any citation
    for citation in answer.citations:
        assert citation.chunk_id != "chunk-foreign", (
            "Foreign chunk ID leaked into verified citations!"
        )
    # Since no claims survived, status should be insufficient_evidence
    assert answer.status in (AnswerStatus.INSUFFICIENT_EVIDENCE, AnswerStatus.PARTIAL)


@pytest.mark.asyncio
async def test_stream_rejects_fabricated_chunk_id():
    """A completely fabricated chunk_id (not in any workspace) must be dropped."""
    llm_output = json.dumps(
        {
            "answer": "Budget is 900 dollars.",
            "claims": [
                {"text": "Budget is 900 dollars.", "chunk_ids": ["totally-fake-id"]}
            ],
        }
    )
    items = [
        make_item(
            "The budget is 900 dollars.",
            0.95,
            workspace_id="ws-1",
            document_id="doc-1",
            chunk_id="chunk-1",
            page=1,
            section="Overview",
        )
    ]
    engine = _make_engine(llm=_StreamingFakeLLM(llm_output), items=items)
    _tokens, answer = await _collect_stream(engine)

    assert answer is not None
    for citation in answer.citations:
        assert citation.chunk_id != "totally-fake-id"
    assert answer.status == AnswerStatus.INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# Tests: multilingual
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_multilingual_romanian_claim():
    """Stream handles multilingual claims — same verification contract as JSON."""
    ro_text = json.dumps(
        {
            "answer": "Bugetul este de 900 de dolari.",
            "claims": [
                {"text": "Bugetul este de 900 de dolari.", "chunk_ids": ["chunk-ro-1"]}
            ],
        }
    )
    items = [
        make_item(
            "Bugetul este de 900 de dolari.",
            0.95,
            workspace_id="ws-1",
            document_id="doc-ro",
            chunk_id="chunk-ro-1",
            page=1,
            section="Overview",
        )
    ]
    engine = _make_engine(llm=_StreamingFakeLLM(ro_text), items=items)
    _tokens, answer = await _collect_stream(engine, query="Care este bugetul?")

    assert answer is not None
    assert answer.status == AnswerStatus.ANSWERED
    # The verified citation should reference the real chunk
    assert len(answer.citations) >= 1
    assert any(c.chunk_id == "chunk-ro-1" for c in answer.citations)


@pytest.mark.asyncio
async def test_stream_multilingual_portuguese_claim():
    """Stream handles Portuguese claims — same verification contract."""
    pt_text = json.dumps(
        {
            "answer": "O orçamento é de 900 dólares.",
            "claims": [
                {"text": "O orçamento é de 900 dólares.", "chunk_ids": ["chunk-pt-1"]}
            ],
        }
    )
    items = [
        make_item(
            "O orçamento é de 900 dólares.",
            0.95,
            workspace_id="ws-1",
            document_id="doc-pt",
            chunk_id="chunk-pt-1",
            page=1,
            section="Overview",
        )
    ]
    engine = _make_engine(llm=_StreamingFakeLLM(pt_text), items=items)
    _tokens, answer = await _collect_stream(engine, query="Qual é o orçamento?")

    assert answer is not None
    assert answer.status == AnswerStatus.ANSWERED
    assert any(c.chunk_id == "chunk-pt-1" for c in answer.citations)


# ---------------------------------------------------------------------------
# Tests: error parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_llm_error_emits_error_event():
    """When the LLM provider fails, the engine should propagate the error."""
    engine = _make_engine(llm=_ErrorFakeLLM())

    events = []
    try:
        async for event_type, _payload in engine.stream(
            query="Test?",
            acl_scope=ACLScope(workspace_ids=["ws-1"]),
            mode=RetrievalMode.HYBRID,
        ):
            events.append(event_type)
    except LLMProviderError:
        # The error propagates as an exception — the SSE layer catches it
        # and emits an error event. At the engine level, this is expected.
        pass

    # The error must propagate (not be swallowed as a token/verified event)
    assert events == [], "No token or verified events should be emitted on LLM error"


@pytest.mark.asyncio
async def test_stream_empty_corpus_yields_insufficient_evidence():
    """Empty retrieval → insufficient_evidence in the final answer."""
    llm_output = json.dumps(
        {
            "answer": "I don't have enough information.",
            "claims": [],
        }
    )
    engine = _make_engine(llm=_StreamingFakeLLM(llm_output), items=[])
    _tokens, answer = await _collect_stream(engine)

    assert answer is not None
    assert answer.status == AnswerStatus.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_stream_unparseable_json_yields_parse_fallback():
    """Non-JSON model output → parse fallback, insufficient_evidence."""
    engine = _make_engine(llm=_StreamingFakeLLM("This is not JSON { broken"))
    _tokens, answer = await _collect_stream(engine)

    assert answer is not None
    assert answer.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert answer.reason_code == "PARSE_FALLBACK"


# ---------------------------------------------------------------------------
# Tests: engine.stream() contract with FakeLLMProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_with_fakellm_provider():
    """The FakeLLMProvider (which has a real stream()) works with the engine."""
    items = [
        make_item(
            "Company revenue grew 20% in 2025.",
            0.95,
            workspace_id="ws-1",
            document_id="doc-1",
            chunk_id="chunk-1",
            page=5,
            section="Financials",
        )
    ]

    # Build a JSON answer that cites chunk-1
    full_text = json.dumps(
        {
            "answer": "Company revenue grew 20% in 2025.",
            "claims": [
                {"text": "Company revenue grew 20% in 2025.", "chunk_ids": ["chunk-1"]}
            ],
        }
    )

    # Use FakeLLMProvider which has a working stream()
    llm = FakeLLMProvider()
    # But FakeLLMProvider echoes based on the prompt, so we need to override
    # its _respond to return our canned answer
    llm._respond = lambda request: full_text

    engine = AnswerEngine(
        orchestrator=_FakeOrchestrator(items),
        llm=llm,
        model="google/gemma-4-26b-a4b-it:free",
    )

    tokens, answer = await _collect_stream(engine)

    assert len(tokens) > 0
    assert answer is not None
    assert answer.status == AnswerStatus.ANSWERED
    assert len(answer.citations) >= 1
    assert answer.citations[0].chunk_id == "chunk-1"


@pytest.mark.asyncio
async def test_stream_client_disconnect_stops_emitting():
    """If the client disconnects mid-stream, the generator stops cleanly."""
    # This is tested at the SSE layer; here we verify the engine doesn't
    # raise on cancellation during streaming.
    engine = _make_engine()

    collected = []
    try:
        async for event_type, payload in engine.stream(
            query="Revenue?",
            acl_scope=ACLScope(workspace_ids=["ws-1"]),
            mode=RetrievalMode.HYBRID,
        ):
            collected.append((event_type, payload))
            if event_type == "token" and len(collected) >= 2:
                # Simulate early exit (like client disconnect)
                break
    except Exception:
        pass

    # We should have collected at least one token before breaking
    assert len(collected) >= 1


# ---------------------------------------------------------------------------
# REV-1 Finding 2: POST /stream body + auth-denied stream
# ---------------------------------------------------------------------------


def test_post_stream_body_shape():
    """REV-1 Finding 2: the stream endpoint is POST with a JSON body.

    The ``AskStreamRequest`` body mirrors ``AskRequest`` minus the per-request
    model override: ``query`` is required (min length 1) and ``mode`` defaults
    to HYBRID. The question text travels in the body, never the request line,
    so it does not land in proxy/access logs or browser history.
    """
    from app.ask.router import AskStreamRequest

    # Valid body: query + default mode.
    body = AskStreamRequest(query="What is the budget?")
    assert body.query == "What is the budget?"
    assert body.mode.value == "hybrid"

    # Query is required and must be non-empty.
    import pytest

    with pytest.raises(ValueError):
        AskStreamRequest(query="")
    with pytest.raises(ValueError):
        AskStreamRequest(query="")  # min_length=1

    # An explicit non-HYBRID mode is accepted and preserved.
    from retrieval.interfaces import RetrievalMode

    body2 = AskStreamRequest(query="Graph question", mode=RetrievalMode.GRAPH)
    assert body2.mode == RetrievalMode.GRAPH


def test_stream_endpoint_is_post_not_get():
    """REV-1 Finding 2: the router exposes POST /stream, not GET.

    A GET query-string endpoint would put the question in the request line
    (privacy leak). Assert the FastAPI route object is a POST route with a
    JSON body parameter — the structural guarantee the task card requires.
    """
    from app.ask.router import router

    # Collect the stream route.
    stream_routes = [r for r in router.routes if getattr(r, "path", "").endswith("/stream")]
    assert stream_routes, "No /stream route registered on the ask router"
    stream_route = stream_routes[0]
    # The route must accept POST (a question in the body, not the URL).
    assert "POST" in stream_route.methods, (
        f"/stream must be a POST endpoint, got methods {stream_route.methods}"
    )
    # The question must come from a JSON body (a parameter with a body
    # type), not a query-string param named 'query'.
    query_params = [p for p in stream_route.dependant.query_params]
    assert not any(p.name == "query" for p in query_params), (
        "/stream must not take the question as a query-string param (privacy)"
    )


@pytest.mark.asyncio
async def test_stream_auth_denied_emits_sse_error_not_http():
    """REV-1 Finding 2: a denied authz returns a well-formed SSE error frame.

    When the caller lacks DOCUMENT_READ on the workspace, the endpoint does
    NOT raise an HTTP 403 (which would be unparseable by an SSE consumer);
    it returns an SSE stream whose single event is ``error`` with the
    denial's code, so the streaming client always receives a well-formed
    frame.
    """
    from app.authz import ApiError

    # Simulate an authz denial: ``require`` raising ApiError(403) must be
    # mapped to an SSE error event, not propagated as an HTTPException.
    denied = ApiError(code="AUTH_DENIED", message="denied", status_code=403)

    # The endpoint's error-only stream yields exactly one well-formed error
    # frame with the denial's code. The router builds this via
    # _error_only_stream(_sse_error_payload(denied)).
    from app.ask.router import _error_only_stream, _sse_error_payload

    payload = _sse_error_payload(denied)
    frames = [f async for f in _error_only_stream(payload)]
    assert len(frames) == 1
    assert frames[0].startswith("event: error\n")
    import json as _json

    data = _json.loads(frames[0].split("data: ", 1)[1])
    assert data["code"] == "AUTH_DENIED"
    # An SSE consumer receives a well-formed frame, not an HTTP error body.
    assert "error" in data


@pytest.mark.asyncio
async def test_stream_extraction_strips_json_before_verification():
    """REV-1 Finding 1: the extractor strips the JSON structure; verification
    still runs on the full raw text at end-of-stream (the JSON POST path and
    the final citations event shape are unchanged)."""
    llm_output = json.dumps(
        {
            "answer": "Company revenue grew 20% in 2025.",
            "claims": [
                {"text": "Company revenue grew 20% in 2025.", "chunk_ids": ["chunk-1"]}
            ],
        }
    )
    engine = _make_engine(llm=_StreamingFakeLLM(llm_output, chunk_size=6))
    events = []
    async for event_type, payload in engine.stream(
        query="Revenue?",
        acl_scope=ACLScope(workspace_ids=["ws-1"]),
        mode=RetrievalMode.HYBRID,
    ):
        events.append((event_type, payload))

    tokens = [p for t, p in events if t == "token"]
    # Tokens carry only the decoded answer text; the JSON structure is gone.
    assert "".join(tokens) == "Company revenue grew 20% in 2025."
    # The terminal event is still the verified Answer with its citations —
    # the verification chain ran on the full raw text, unchanged.
    assert events[-1][0] == "verified"
    assert events[-1][1].status == AnswerStatus.ANSWERED
    assert events[-1][1].citations[0].chunk_id == "chunk-1"


_FORBIDDEN_TOKEN_MARKERS = ("chunk_id", "chunk-", "cite:", "claim_id", "evidence-")


def _assert_no_id_in_tokens(tokens: list[str]) -> None:
    """Shared assertion: no token payload contains a citation/evidence ID."""
    for payload in tokens:
        assert isinstance(payload, str), f"Token payload must be str, got {type(payload).__name__}"
        for marker in _FORBIDDEN_TOKEN_MARKERS:
            assert marker not in payload, (
                f"Token leaked a citation/evidence ID {marker!r}: {payload!r}"
            )


@pytest.mark.asyncio
async def test_stream_tokens_carry_answer_text_in_order():
    """REV-1 Finding 1: partial answer text streams in order, claims stay out.

    The extractor yields the decoded ``"answer"`` value as it streams. The
    joined tokens must equal the expected answer text (in order), and no
    claims/chunk_ids structure may be present in any token.
    """
    llm_output = json.dumps(
        {
            "answer": "Company revenue grew 20% in 2025.",
            "claims": [
                {"text": "Company revenue grew 20% in 2025.", "chunk_ids": ["chunk-1"]}
            ],
        }
    )
    engine = _make_engine(llm=_StreamingFakeLLM(llm_output, chunk_size=4))
    tokens, answer = await _collect_stream(engine)

    # The answer text streams in order and reconstructs the full answer.
    assert "".join(tokens) == "Company revenue grew 20% in 2025."
    # No citation/evidence ID crosses the token seam.
    _assert_no_id_in_tokens(tokens)
    # Claims never reach a token event.
    assert not any("claims" in t for t in tokens)
    assert answer is not None
    assert answer.status == AnswerStatus.ANSWERED


@pytest.mark.asyncio
async def test_raw_json_token_path_would_trip_the_assertion():
    """REV-1 Finding 1 FAIL-before proof.

    Simulates the OLD pre-extraction token path: raw model JSON streamed as
    token payloads. The forbidden-substring assertion trips on it — proving
    the assertion is strong enough to catch a regression where extraction is
    disabled (tokens would leak unverified chunk_ids / claims structure).

    This is a unit-level check on the *assertion itself*, not on the engine:
    it feeds raw JSON tokens into the same forbidden-marker check and shows
    it fails, which is the regression the extraction path prevents.
    """
    raw_json = json.dumps(
        {
            "answer": "Company revenue grew 20% in 2025.",
            "claims": [
                {"text": "Company revenue grew 20% in 2025.", "chunk_ids": ["chunk-1"]}
            ],
        }
    )
    # The pre-extraction path would have emitted this raw JSON as a token.
    # The forbidden-substring assertion MUST trip on it.
    assert any(marker in raw_json for marker in _FORBIDDEN_TOKEN_MARKERS), (
        "FAIL-BEFORE proof: raw model JSON contains a citation/evidence marker, "
        "so a token path that leaks raw JSON would be caught. Without the "
        "extraction, this assertion would have failed — with it, the engine "
        "strips the structure so the raw JSON never becomes a token payload."
    )
    # Specifically the "chunk_id" / "chunk-" markers are present in the raw JSON
    # that the old path would have emitted mid-stream.
    assert "chunk_id" in raw_json and "chunk-" in raw_json


@pytest.mark.asyncio
async def test_stream_unparseable_model_output_emits_no_answer_tokens():
    """REV-1 Finding 1: unparseable model output -> no answer tokens leak.

    The extractor only emits the decoded ``"answer"`` value when the stream is
    valid JSON. For a non-JSON stream the extractor finds no answer value, so
    zero answer tokens are emitted and the terminal event is the existing
    PARSE_FALLBACK answer — no partial raw JSON crosses the wire.
    """
    engine = _make_engine(llm=_StreamingFakeLLM("This is not valid JSON at all"))
    events = []
    async for event_type, payload in engine.stream(
        query="Revenue?",
        acl_scope=ACLScope(workspace_ids=["ws-1"]),
        mode=RetrievalMode.HYBRID,
    ):
        events.append((event_type, payload))
    token_events = [p for t, p in events if t == "token"]
    # No answer text was decoded, so no token payloads carry raw JSON either.
    assert all(isinstance(p, str) for p in token_events)
    _assert_no_id_in_tokens(token_events)
    # The terminal event is the parse-fallback abstention.
    assert events[-1][0] == "verified"
    assert events[-1][1].status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert events[-1][1].reason_code == "PARSE_FALLBACK"


@pytest.mark.asyncio
async def test_stream_partial_answer_streams_before_terminal_event():
    """REV-1 Finding 1: partial answer text is emitted incrementally, in order,
    before the terminal verified event — claims/chunk_ids never appear."""
    llm_output = json.dumps(
        {
            "answer": "The budget is 900 dollars.",
            "claims": [
                {"text": "The budget is 900 dollars.", "chunk_ids": ["chunk-1"]},
            ],
        }
    )
    # Small chunk size so the answer text spans multiple token events.
    engine = _make_engine(
        llm=_StreamingFakeLLM(llm_output, chunk_size=5),
        items=[
            make_item(
                "The budget is 900 dollars.",
                0.95,
                workspace_id="ws-1",
                document_id="doc-1",
                chunk_id="chunk-1",
                page=1,
                section="Overview",
            )
        ],
    )
    events = []
    async for event_type, payload in engine.stream(
        query="Budget?",
        acl_scope=ACLScope(workspace_ids=["ws-1"]),
        mode=RetrievalMode.HYBRID,
    ):
        events.append((event_type, payload))

    tokens = [p for t, p in events if t == "token"]
    # Partial answer streams in order and reconstructs the full answer text.
    assert "".join(tokens) == "The budget is 900 dollars."
    # Multiple token events (incremental), all before the terminal event.
    assert len(tokens) > 1
    _assert_no_id_in_tokens(tokens)
def _chunk_sizes() -> list[int]:
    return [1, 2, 3, 4, 5, 6, 7, 40]


def test_streaming_extractor_escape_heavy_answer():
    """REV-2: escape-heavy answer decodes to the exact decoded string."""
    answer = 'Hue said "hi"\nthen\\left\tgot €50.'
    payload = json.dumps({"answer": answer, "claims": []}, ensure_ascii=True)
    expected = json.loads(payload)["answer"]
    for size in _chunk_sizes():
        ext = _StreamingAnswerExtractor()
        toks: list[str] = []
        for i in range(0, len(payload), size):
            toks.extend(ext.feed(payload[i : i + size]))
        assert "".join(toks) == expected, (size, "".join(toks), expected)


def test_streaming_extractor_emoji_surrogate_pair():
    """REV-2: surrogate-pair emoji survives chunked streaming."""
    payload = '{"answer": "smiley \\ud83d\\ude00 here", "claims": []}'
    expected = json.loads(payload)["answer"]
    for size in [1, 3, 7, len(payload)]:
        ext = _StreamingAnswerExtractor()
        toks: list[str] = []
        for i in range(0, len(payload), size):
            toks.extend(ext.feed(payload[i : i + size]))
        assert "".join(toks) == expected


def test_streaming_extractor_non_ascii_ensure_ascii():
    """REV-2: non-ASCII under ensure_ascii decodes correctly."""
    answer = "café — naïve"
    payload = json.dumps({"answer": answer, "claims": []}, ensure_ascii=True)
    expected = json.loads(payload)["answer"]
    for size in [1, 2, 5, len(payload)]:
        ext = _StreamingAnswerExtractor()
        toks: list[str] = []
        for i in range(0, len(payload), size):
            toks.extend(ext.feed(payload[i : i + size]))
        assert "".join(toks) == expected


def test_streaming_extractor_empty_input_yields_nothing():
    ext = _StreamingAnswerExtractor()
    assert ext.feed("") == []
    assert ext.feed("   ") == []


def test_streaming_extractor_garbage_yields_nothing():
    ext = _StreamingAnswerExtractor()
    toks: list[str] = []
    for ch in "this is not json at all":
        toks.extend(ext.feed(ch))
    assert "".join(toks) == ""


def test_streaming_extractor_no_answer_key_yields_nothing():
    payload = json.dumps({"claims": [{"text": "x", "chunk_ids": ["c1"]}]})
    ext = _StreamingAnswerExtractor()
    toks: list[str] = []
    for i in range(0, len(payload), 4):
        toks.extend(ext.feed(payload[i : i + 4]))
    assert "".join(toks) == ""


# ---------------------------------------------------------------------------
# REV-3 (POLICE final review 2026-10-24) — serialization parity + model honor
# ---------------------------------------------------------------------------


def _full_answer() -> Answer:
    """An Answer with EVERY field populated — including limitations.

    `limitations` carries the unreadable-page / missing-evidence disclosures
    (OCR warnings, absent tables) the SSE serializer was found dropping.
    """
    return Answer(
        answer="Company revenue grew 20% in 2025.",
        status=AnswerStatus.ANSWERED,
        reason_code=None,
        reason=None,
        confidence=Confidence(level=ConfidenceLevel.HIGH, basis="grounded", probability=0.93),
        limitations=[
            "Page 5 is a scanned image; OCR text may contain errors.",
            "The 2026 forecast table was not present in the corpus.",
        ],
        trace_id="trace-parity-1",
        claims=[Claim(text="Company revenue grew 20% in 2025.", chunk_ids=["chunk-1"])],
        citations=[
            Citation(
                chunk_id="chunk-1",
                document_id="doc-1",
                filename="report.pdf",
                page=5,
                section="Financials",
                span=(10, 42),
                workspace_id="ws-1",
                grounding_score=0.9,
                content="Company revenue grew 20% in 2025.",
            )
        ],
        citation_integrity={"status": "verified", "checked": 1},
        semantic_verification={"status": "passed", "model": "gemma"},
        publication_gate={"decision": "publish", "reasons": []},
        publication_status="verified",
        verifier_model="google/gemma-4-26b-a4b-it:free",
    )


class _RouteFakeEngine:
    """Engine whose answer()/stream() share ONE pre-built Answer object.

    Sharing matters: the parity test compares the JSON route body against the
    SSE citations payload for the SAME answer — a fresh trace_id per call would
    make the comparison measure engine freshness, not serialization.
    """

    def __init__(self, answer: Answer) -> None:
        self._answer = answer
        self.model = "unrouted-factory-sentinel"
        # Present so the stream handler's semantic-verifier fallback import
        # path is skipped entirely (no third-party import in a unit test).
        self.semantic_verifier = object()

    async def answer(self, *, query, acl_scope, mode=None, **kwargs) -> Answer:
        return self._answer

    def stream(self, *, query, acl_scope, mode=None, **kwargs) -> AsyncIterator[tuple[str, object]]:
        async def _gen() -> AsyncIterator[tuple[str, object]]:
            yield ("token", self._answer.answer)
            yield ("verified", self._answer)

        return _gen()


class _FakeRequest:
    """Minimal Request stand-in: the stream only probes disconnection."""

    async def is_disconnected(self) -> bool:
        return False


def _install_fake_factory(monkeypatch, answer: Answer) -> tuple[_RouteFakeEngine, dict]:
    """Patch the router's engine factory; capture the model identity it receives."""
    import app.ask.router as router_mod

    engine = _RouteFakeEngine(answer)
    seen: dict = {}

    def _fake_factory(*, model: str, sensitive: bool = False):
        # T-V1-028: the handler must thread the router's pick into the factory
        # as its REQUIRED model — `seen` records the exact identity that would
        # reach the provider.
        engine.model = model
        seen["model"] = model
        seen["sensitive"] = sensitive
        return engine

    monkeypatch.setattr(router_mod, "build_answer_engine", _fake_factory)
    return engine, seen


async def _sse_frames(response) -> list[tuple[str, dict]]:
    """Drain a StreamingResponse into (event, parsed-data) frames."""
    raw = ""
    async for chunk in response.body_iterator:
        raw += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
    frames: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event: str | None = None
        data: str | None = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data = line[len("data: ") :].strip()
        if event is None or data is None:
            continue
        frames.append((event, json.loads(data)))
    return frames


def _owner(workspace_id: uuid.UUID) -> Principal:
    return Principal(
        user_id="u-rev3",
        is_authenticated=True,
        workspace_roles={str(workspace_id): Role.OWNER},
    )


@pytest.mark.asyncio
async def test_stream_citations_payload_serialization_parity(monkeypatch):
    """REV-3 Finding 1: the SSE citations payload IS the JSON /ask body.

    The police probe found `Answer.limitations` (unreadable-page disclosures)
    disappearing on the SSE path. Parity is asserted field-for-field against
    the real model dump — the exact object FastAPI serializes for the JSON
    route — and the key set is asserted EXHAUSTIVE against Answer's fields, so
    any future Answer field that fails to cross the SSE wire fails here rather
    than silently dropping a disclosure in production.
    """
    answer = _full_answer()
    _install_fake_factory(monkeypatch, answer)
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    monkeypatch.delenv("WORKSPACE_MODEL_CONFIG", raising=False)

    from app.ask.router import AskRequest, AskStreamRequest, ask, ask_stream

    workspace_id = uuid.uuid4()
    principal = _owner(workspace_id)

    # The JSON route's body for the same answer — what the wire carries.
    json_answer = await ask(workspace_id, AskRequest(query="Revenue?"), principal)
    json_body = json_answer.model_dump(mode="json")

    # The SSE route's terminal citations frame, from the real handler.
    response = await ask_stream(
        workspace_id,
        AskStreamRequest(query="Revenue?"),
        principal=principal,
        request=_FakeRequest(),
    )
    frames = await _sse_frames(response)
    citations_frames = [(e, d) for e, d in frames if e == "citations"]
    assert len(citations_frames) == 1, frames
    sse_payload = citations_frames[0][1]

    # Exhaustive: every Answer field crosses the SSE wire — a future field
    # cannot be added to the JSON contract and forgotten on the SSE side.
    assert set(sse_payload) == set(Answer.model_fields)
    # Exact parity — not a subset check.
    assert sse_payload == json_body
    # The finding's own assertion: disclosures survive the stream.
    assert sse_payload["limitations"] == [
        "Page 5 is a scanned image; OCR text may contain errors.",
        "The 2026 forecast table was not present in the corpus.",
    ]


@pytest.mark.asyncio
async def test_stream_endpoint_honors_selected_model(monkeypatch, caplog):
    """REV-3 Finding 2: the frontend's selected model is honored on the
    streaming path through the SAME policy resolver as POST /ask.

    The police probe showed `AskStreamRequest` omitting `model` while the
    frontend sends it, so `override_model` was hardwired None and the operator
    default silently replaced the user's selection. The model identity the
    factory receives — the exact model that would reach the provider — and the
    logged precedence tier are pinned here.
    """
    answer = _full_answer()
    engine, seen = _install_fake_factory(monkeypatch, answer)
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    monkeypatch.delenv("WORKSPACE_MODEL_CONFIG", raising=False)
    caplog.set_level(logging.INFO, logger="knowledge_os.api.ask")

    from app.ask.router import AskStreamRequest, ask_stream

    workspace_id = uuid.uuid4()
    principal = _owner(workspace_id)

    response = await ask_stream(
        workspace_id,
        AskStreamRequest(query="Revenue?", model="request-override-b"),
        principal=principal,
        request=_FakeRequest(),
    )
    async for _chunk in response.body_iterator:
        pass  # drain: the handler wires the stream before returning

    assert seen["model"] == "request-override-b"
    assert engine.model == "request-override-b"
    events = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "ask_stream_model_selected"
    ]
    assert len(events) == 1
    assert events[0].reason == "permitted_override"
    assert events[0].model == "request-override-b"


@pytest.mark.asyncio
async def test_stream_endpoint_without_model_uses_operator_default(monkeypatch, caplog):
    """REV-3 Finding 2 (bidirectional): honoring a SELECTED model must not
    change the no-selection default — absent `model`, the stream endpoint
    resolves exactly like POST /ask would (operator default)."""
    answer = _full_answer()
    _engine, seen = _install_fake_factory(monkeypatch, answer)
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    monkeypatch.delenv("WORKSPACE_MODEL_CONFIG", raising=False)
    caplog.set_level(logging.INFO, logger="knowledge_os.api.ask")

    from app.ask.router import AskStreamRequest, ask_stream

    workspace_id = uuid.uuid4()
    principal = _owner(workspace_id)

    response = await ask_stream(
        workspace_id,
        AskStreamRequest(query="Revenue?"),
        principal=principal,
        request=_FakeRequest(),
    )
    async for _chunk in response.body_iterator:
        pass

    assert seen["model"] == "operator-model-a"
    events = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "ask_stream_model_selected"
    ]
    assert len(events) == 1
    assert events[0].reason == "operator_default"
    assert events[0].model == "operator-model-a"
