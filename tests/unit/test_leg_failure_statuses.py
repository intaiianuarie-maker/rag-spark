"""T-V1-026: traces + honest failure statuses per leg (RP-11 / QF-11).

"No results" must be distinguishable from "search is down", with a tracking ID.
Every mandatory leg gets a failure injection here — **embedding**, **retrieval**
(dense store and lexical store), **rerank** and **generation** — and the
empty-corpus case is pinned as a *success* with zero items, never an error.

All on fakes: no services, no network, no clock, no randomness.

Contract locked by these tests:

* a **required** leg failing (``REQUIRED_LEGS``) → ``status="unavailable"``,
  the registered safe reason, ``items`` discarded, ``trace_id`` present;
* an **optional** leg failing (``sparse`` / ``graph`` / ``rerank``) →
  ``status="degraded"``, items intact, degradation disclosed;
* every leg runs cleanly and finds nothing → ``status="ok"`` + ``items == []``
  (**empty corpus is not a failure**);
* no surfaced trace carries exception text, SQL, hostnames or prompt content —
  only the exception class name and the registry's safe message.
"""

from __future__ import annotations

import os
import uuid

import pytest
from core.auth import ACLScope
from core.errors import AnswerStatus, ReasonCode, canonical_code, error_detail
from harness_fakes import (
    FakeEmbeddingProvider,
    FakeLexicalStore,
    FakeVectorStore,
    make_record,
)
from retrieval.interfaces import (
    EmbeddingProvider,
    LexicalStore,
    Reranker,
    RetrievalMode,
    RetrievedItem,
    SearchFilter,
    VectorStore,
)
from retrieval.orchestrator import (
    REQUIRED_LEGS,
    HybridRetrievalOrchestrator,
    RetrievalStatus,
    pop_retrieval_outcome,
)

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.unit

COLLECTION = "legfail"
DIMENSION = 8
SCOPE = ACLScope(user_id="u1", workspace_ids=["ws-a"])

#: Strings that must never appear in a user-facing reason (safe-message lint).
UNSAFE = ("http://", "https://", "localhost", "127.0.0.1", "traceback", "select ")


# --- failure fakes (one per injected leg) -----------------------------------


class _DownEmbedding(EmbeddingProvider):
    """Embeddings service down (infinity /v1/embeddings unreachable)."""

    model_name = "down-embed"
    dimension = DIMENSION

    async def embed_documents(self, texts):
        raise ConnectionError("infinity embeddings refused")

    async def embed_query(self, text):
        raise ConnectionError("infinity embeddings refused")


class _DownVectorStore(VectorStore):
    """Dense store down (Qdrant unreachable) — embedding still works."""

    def __init__(self) -> None:
        self.dimension = DIMENSION

    async def ensure_collection(self, *, collection, dimension, distance="cosine", recreate=False):
        self.dimension = dimension

    async def upsert(self, *, collection, records):
        raise AssertionError("this store is read-only in the fixture")

    async def search(self, *, collection, vector, top_k, query_filter):
        raise TimeoutError("qdrant search timed out")

    async def delete(self, *, collection, ids=None, query_filter=None):
        return 0


class _DownLexicalStore(LexicalStore):
    """Lexical store down (PostgreSQL FTS unreachable)."""

    async def index(self, *, records):
        raise AssertionError("this store is read-only in the fixture")

    async def search(self, *, query, top_k, query_filter):
        raise ConnectionError("postgres FTS refused connection")

    async def delete(self, *, ids=None, query_filter=None):
        return 0


class _DownReranker(Reranker):
    """Reranker down (optional leg)."""

    model_name = "down-reranker"

    async def rerank(self, *, query, documents, top_k=None):
        raise RuntimeError("infinity /rerank returned 503")


async def _records(workspace_id: str = "ws-a") -> list:
    def vector(*head: float) -> list[float]:
        return [*head, *([0.0] * (DIMENSION - len(head)))]

    return [
        make_record(
            "a-1",
            workspace_id,
            "hybrid retrieval evidence about permissions",
            vector=vector(1.0),
        ),
        make_record(
            "a-2", workspace_id, "hybrid retrieval ranks evidence", vector=vector(0.8, 0.2)
        ),
    ]


async def _orchestrator(
    *,
    dense: VectorStore | None = None,
    lexical: LexicalStore | None = None,
    embedding: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
    sparse=None,
    workspace_id: str = "ws-a",
    seed: bool = True,
) -> HybridRetrievalOrchestrator:
    """Real orchestrator over a mix of healthy and injected-broken legs.

    ``seed=False`` builds a genuinely **empty corpus** (real stores, zero
    records) — the empty-corpus-is-not-a-failure case.
    """
    store = dense if dense is not None else FakeVectorStore(dimension=DIMENSION)
    await store.ensure_collection(collection=COLLECTION, dimension=DIMENSION)
    records = await _records(workspace_id)
    if seed and not isinstance(store, _DownVectorStore):
        await store.upsert(collection=COLLECTION, records=records)
    lex = lexical if lexical is not None else FakeLexicalStore()
    if seed and not isinstance(lex, _DownLexicalStore):
        await lex.index(records=records)
    return HybridRetrievalOrchestrator(
        dense=store,
        embedding=embedding or FakeEmbeddingProvider(),
        lexical=lex,
        collection=COLLECTION,
        sparse=sparse,
        reranker=reranker,
    )


# --- 1. per-leg failure injection (the ticket's metric: 4/4) -----------------


async def test_injected_embedding_failure_is_unavailable_with_tracking_id() -> None:
    orchestrator = await _orchestrator(embedding=_DownEmbedding())
    result = await orchestrator.retrieve_result(query="hybrid retrieval", acl_scope=SCOPE)
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.reason_code is ReasonCode.EMBED_UNAVAILABLE
    assert result.items == [], "nothing was searched — no partial answer may escape"
    assert result.trace_id
    assert [failure.leg for failure in result.legs_failed] == ["embedding"]
    assert result.legs_failed[0].error_type == "ConnectionError"
    assert result.reason == error_detail(ReasonCode.EMBED_UNAVAILABLE)["message"]


async def test_injected_dense_retrieval_failure_is_unavailable_with_tracking_id() -> None:
    orchestrator = await _orchestrator(dense=_DownVectorStore())
    result = await orchestrator.retrieve_result(query="hybrid retrieval", acl_scope=SCOPE)
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.reason_code is ReasonCode.RETRIEVAL_UNAVAILABLE
    assert result.items == []
    assert result.trace_id
    assert [failure.leg for failure in result.legs_failed] == ["dense"]
    assert result.legs_failed[0].error_type == "TimeoutError"


async def test_injected_lexical_retrieval_failure_is_unavailable_with_tracking_id() -> None:
    orchestrator = await _orchestrator(lexical=_DownLexicalStore())
    result = await orchestrator.retrieve_result(query="hybrid retrieval", acl_scope=SCOPE)
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.reason_code is ReasonCode.RETRIEVAL_UNAVAILABLE
    assert result.items == []
    assert result.trace_id
    assert [failure.leg for failure in result.legs_failed] == ["lexical"]


async def test_injected_rerank_failure_degrades_but_keeps_results() -> None:
    """The reranker is OPTIONAL: results survive, the degradation is disclosed."""
    orchestrator = await _orchestrator(reranker=_DownReranker())
    result = await orchestrator.retrieve_result(query="hybrid retrieval", acl_scope=SCOPE)
    assert result.items, "an optional leg must never take the answer down"
    assert result.status is RetrievalStatus.DEGRADED
    assert result.reason_code is ReasonCode.RERANK_UNAVAILABLE
    assert result.reason == error_detail(ReasonCode.RERANK_UNAVAILABLE)["message"]
    assert result.trace_id
    assert [failure.leg for failure in result.legs_failed] == ["rerank"]


async def test_all_four_injected_legs_report_a_tracking_id() -> None:
    """Metric: injected failures with tracking IDs = 4/4."""
    cases = [
        await _orchestrator(embedding=_DownEmbedding()),
        await _orchestrator(dense=_DownVectorStore()),
        await _orchestrator(lexical=_DownLexicalStore()),
        await _orchestrator(reranker=_DownReranker()),
    ]
    results = [await o.retrieve_result(query="hybrid retrieval", acl_scope=SCOPE) for o in cases]
    assert len({result.trace_id for result in results}) == 4, "tracking IDs must be unique"
    assert all(result.trace_id for result in results)
    assert all(result.legs_failed for result in results)


# --- 2. empty corpus is NOT a failure ----------------------------------------


async def test_empty_corpus_returns_empty_with_ok_status() -> None:
    """Metric: the empty-corpus case still returns empty (not an error)."""
    orchestrator = HybridRetrievalOrchestrator(
        dense=FakeVectorStore(dimension=DIMENSION),
        embedding=FakeEmbeddingProvider(),
        lexical=FakeLexicalStore(),
        collection=COLLECTION,
    )
    result = await orchestrator.retrieve_result(query="nothing matches this", acl_scope=SCOPE)
    assert result.items == []
    assert result.status is RetrievalStatus.OK
    assert result.reason_code is None
    assert result.reason == ""
    assert result.legs_failed == ()


async def test_empty_scope_and_blank_query_are_ok_not_unavailable() -> None:
    """Fail-closed guards are *no attempts*, so they can never read as outages."""
    orchestrator = await _orchestrator()
    empty_scope = await orchestrator.retrieve_result(
        query="anything", acl_scope=ACLScope(user_id=None, workspace_ids=())
    )
    assert empty_scope.status is RetrievalStatus.OK and empty_scope.items == []
    blank = await orchestrator.retrieve_result(query="   ", acl_scope=SCOPE)
    assert blank.status is RetrievalStatus.OK and blank.items == []


# --- 3. no silent-empty conversion anywhere ----------------------------------


@pytest.mark.parametrize(
    ("mode", "broken", "expected_leg"),
    [
        (RetrievalMode.SEMANTIC, "embedding", "embedding"),
        (RetrievalMode.SEMANTIC, "dense", "dense"),
        (RetrievalMode.KEYWORD, "lexical", "lexical"),
        (RetrievalMode.HYBRID, "lexical", "lexical"),
        (RetrievalMode.HYBRID, "dense", "dense"),
    ],
)
async def test_every_mode_reports_required_leg_failures(
    mode: RetrievalMode, broken: str, expected_leg: str
) -> None:
    """Zero legs convert a failure into a silent empty list, in every mode."""
    overrides: dict = {
        "embedding": {"embedding": _DownEmbedding()},
        "dense": {"dense": _DownVectorStore()},
        "lexical": {"lexical": _DownLexicalStore()},
    }
    orchestrator = await _orchestrator(**overrides[broken])
    result = await orchestrator.retrieve_result(
        query="hybrid retrieval", acl_scope=SCOPE, mode=mode
    )
    assert expected_leg in REQUIRED_LEGS[mode.value], "injection must target a required leg"
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.items == []
    assert [failure.leg for failure in result.legs_failed] == [expected_leg]
    assert canonical_code(result.reason_code.value) is result.reason_code


async def test_silent_leg_failure_leaves_no_trace() -> None:
    """Metric: legs with silent-empty conversion = 0."""
    for orchestrator in (
        await _orchestrator(dense=_DownVectorStore()),
        await _orchestrator(lexical=_DownLexicalStore()),
        await _orchestrator(embedding=_DownEmbedding()),
        await _orchestrator(reranker=_DownReranker()),
    ):
        result = await orchestrator.retrieve_result(query="hybrid retrieval", acl_scope=SCOPE)
        assert result.legs_failed, "a failing leg must always be reported"


# --- 4. surfaced traces are safe (no prompt/secret/exception content) --------


async def test_surfaced_trace_carries_no_exception_text() -> None:
    orchestrator = await _orchestrator(lexical=_DownLexicalStore())
    result = await orchestrator.retrieve_result(query="hybrid retrieval", acl_scope=SCOPE)
    reason = result.reason.lower()
    for banned in (*UNSAFE, "postgres", "refused", "hybrid retrieval"):
        assert banned not in reason, f"reason leaks internals: {banned!r}"
    for failure in result.legs_failed:
        assert failure.error_type == "ConnectionError"
        assert " " not in failure.error_type, "only a class name, never a message"


# --- 5. trace context is propagated, and consumed exactly once ---------------


async def test_retrieve_publishes_the_outcome_for_the_calling_layer() -> None:
    """``retrieve()`` keeps its list contract AND publishes the honest outcome."""
    healthy = await _orchestrator()
    items = await healthy.retrieve(query="hybrid retrieval", acl_scope=SCOPE)
    outcome = pop_retrieval_outcome()
    assert outcome is not None and outcome.items is items
    assert outcome.status is RetrievalStatus.OK
    # Consumed exactly once: a later call can never read a stale outcome.
    assert pop_retrieval_outcome() is None

    broken = await _orchestrator(lexical=_DownLexicalStore())
    assert await broken.retrieve(query="hybrid retrieval", acl_scope=SCOPE) == []
    outcome = pop_retrieval_outcome()
    assert outcome is not None and outcome.status is RetrievalStatus.UNAVAILABLE
    assert outcome.reason_code is ReasonCode.RETRIEVAL_UNAVAILABLE


async def test_tracking_id_follows_the_request_id_context() -> None:
    """The API's x-request-id becomes the tracking ID (ingress → retrieval)."""
    from core.logging import reset_request_id, set_request_id

    token = set_request_id("req-trace-abc123")
    try:
        orchestrator = await _orchestrator(lexical=_DownLexicalStore())
        result = await orchestrator.retrieve_result(query="hybrid retrieval", acl_scope=SCOPE)
    finally:
        reset_request_id(token)
    assert result.trace_id == "req-trace-abc123"


# --- 6. answer path: generation leg + retrieval status mapping ---------------


class _FakeLLM:
    """Generation-leg double: returns one verifiable claim, or raises like a down gateway."""

    provider_name = "fake"

    #: Claim text is verbatim inside retrieved chunk ``a-1`` (see
    #: :func:`_records`), so it verifies under the legacy existence/scope
    #: contract too.
    TEXT = (
        '{"answer": "permissions are enforced", "claims": '
        '[{"text": "permissions are enforced", "chunk_ids": ["a-1"]}]}'
    )

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def generate(self, request):
        from models.interfaces import GenerationResult

        self.calls += 1
        if self.fail:
            raise ConnectionError("llm gateway refused")
        return GenerationResult(
            text=self.TEXT,
            model=request.model,
            finish_reason="stop",
            prompt_tokens=0,
            completion_tokens=6,
        )


async def test_generation_failure_is_never_swallowed_into_an_abstention() -> None:
    """A down gateway must not look like 'insufficient evidence'."""
    from answer.engine import AnswerEngine

    engine = AnswerEngine(
        orchestrator=await _orchestrator(), llm=_FakeLLM(fail=True), model="test-model"
    )
    with pytest.raises(ConnectionError):
        await engine.answer(query="hybrid retrieval", acl_scope=SCOPE)


async def test_retrieval_unavailable_short_circuits_before_generation() -> None:
    """The model is never called on an unsearched corpus (no wasted tokens)."""
    from answer.engine import AnswerEngine

    llm = _FakeLLM()
    engine = AnswerEngine(
        orchestrator=await _orchestrator(lexical=_DownLexicalStore()),
        llm=llm,
        model="test-model",
    )
    answer = await engine.answer(query="hybrid retrieval", acl_scope=SCOPE)
    assert llm.calls == 0, "generation must not run when nothing was retrieved"
    assert answer.status is AnswerStatus.UNAVAILABLE
    assert answer.reason_code == ReasonCode.RETRIEVAL_UNAVAILABLE.value
    assert answer.reason
    assert answer.trace_id
    assert canonical_code(answer.reason_code) is not None


async def test_retrieval_degraded_downgrades_a_standing_answer() -> None:
    """Optional leg down + a grounded answer ⇒ ``degraded``, disclosed."""
    from answer.engine import AnswerEngine

    llm = _FakeLLM()
    engine = AnswerEngine(
        orchestrator=await _orchestrator(reranker=_DownReranker()),
        llm=llm,
        model="test-model",
        claim_grounder=None,
    )
    answer = await engine.answer(query="hybrid retrieval", acl_scope=SCOPE)
    assert answer.status is AnswerStatus.DEGRADED
    assert answer.reason_code == ReasonCode.RERANK_UNAVAILABLE.value
    assert answer.reason
    assert answer.trace_id
    assert answer.citations, "the answer still stands and keeps its citations"


async def test_empty_corpus_answer_is_a_normal_abstention_not_unavailable() -> None:
    """Empty corpus ⇒ ``insufficient_evidence`` + EVIDENCE_MISSING (unchanged)."""
    from answer.engine import AnswerEngine

    engine = AnswerEngine(
        orchestrator=HybridRetrievalOrchestrator(
            dense=FakeVectorStore(dimension=DIMENSION),
            embedding=FakeEmbeddingProvider(),
            lexical=FakeLexicalStore(),
            collection=COLLECTION,
        ),
        llm=_FakeLLM(),
        model="test-model",
    )
    answer = await engine.answer(query="nothing matches this", acl_scope=SCOPE)
    assert answer.status is AnswerStatus.INSUFFICIENT_EVIDENCE
    assert answer.reason_code == ReasonCode.EVIDENCE_MISSING.value


# --- 7. HTTP boundary: search and ask distinguish the two failures -----------


def _principal(user_id: str = "user-1"):
    from core.auth import Principal, Role

    return Principal(
        user_id=user_id,
        email=f"{user_id}@example.test",
        is_authenticated=True,
        workspace_roles={WS_ID: Role.EDITOR},
    )


WS_ID = str(uuid.uuid4())


WS_ID = str(uuid.uuid4())


def _build(**kwargs):
    """Build the real orchestrator on fakes from a sync test (no services).

    Records land in ``WS_ID`` so the route's ACL scope (which always pins the
    caller's workspace) keeps them — the HTTP tests exercise the real filter.
    """
    import asyncio

    kwargs.setdefault("workspace_id", WS_ID)
    return asyncio.run(_orchestrator(**kwargs))


def _client(monkeypatch, orchestrator):
    import app.search.search as search_module
    from app.authz.principal import get_principal
    from app.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(search_module, "create_orchestrator", lambda: orchestrator)
    app = create_app()
    app.dependency_overrides[get_principal] = lambda: _principal()
    return TestClient(app)


def test_search_returns_503_with_tracking_id_when_a_required_leg_is_down(
    monkeypatch,
) -> None:
    client = _client(monkeypatch, _build(lexical=_DownLexicalStore()))
    response = client.post(f"/v1/workspaces/{WS_ID}/search", json={"query": "permissions"})
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "RETRIEVAL_UNAVAILABLE"  # canonical wire value (T-V1-025)
    assert detail["trace_id"], "the 503 must carry a tracking ID"
    assert detail["message"]


def test_search_returns_200_empty_ok_for_an_empty_corpus(monkeypatch) -> None:
    """The empty corpus is a 200 with ``ok``, never a 503."""
    client = _client(monkeypatch, _build(seed=False))
    response = client.post(f"/v1/workspaces/{WS_ID}/search", json={"query": "zzz nothing"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"] == []
    assert body["status"] == "ok"
    assert body["reason_code"] is None
    assert body["trace_id"], "even a healthy run is trackable"


def test_search_discloses_a_degraded_optional_leg(monkeypatch) -> None:
    client = _client(monkeypatch, _build(reranker=_DownReranker()))
    response = client.post(f"/v1/workspaces/{WS_ID}/search", json={"query": "hybrid retrieval"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"], "results survive an optional-leg outage"
    assert body["status"] == "degraded"
    assert body["reason_code"] == "RERANK_UNAVAILABLE"  # canonical, not legacy lowercase
    assert body["trace_id"]


def test_ask_maps_retrieval_unavailable_to_503_with_tracking_id(
    monkeypatch,
) -> None:
    """Ask and Search speak the same failure, at the same status."""
    import app.ask.router as router
    from app.authz.principal import get_principal
    from app.main import create_app
    from fastapi.testclient import TestClient

    class _Engine:
        model = "down"

        async def answer(self, *, query, acl_scope, mode=None, **kwargs):
            from answer.models import Answer, AnswerStatus, Confidence, ConfidenceLevel

            return Answer(
                answer="I cannot answer this question right now.",
                status=AnswerStatus.UNAVAILABLE,
                reason_code=ReasonCode.RETRIEVAL_UNAVAILABLE.value,
                reason=error_detail(ReasonCode.RETRIEVAL_UNAVAILABLE)["message"],
                confidence=Confidence(level=ConfidenceLevel.LOW, basis="leg down"),
                trace_id="trace-ask-1",
            )

    monkeypatch.setattr(router, "build_answer_engine", lambda **_: _Engine())
    app = create_app()
    app.dependency_overrides[get_principal] = lambda: _principal()
    client = TestClient(app)
    response = client.post(
        f"/v1/workspaces/{WS_ID}/ask", json={"query": "what governs access?"}
    )
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "RETRIEVAL_UNAVAILABLE"  # canonical wire value (T-V1-025)
    assert detail["trace_id"] == "trace-ask-1"


def test_ask_empty_corpus_stays_a_200_abstention(monkeypatch) -> None:
    """The empty corpus is not an HTTP failure on Ask either."""
    import app.ask.router as router
    from app.authz.principal import get_principal
    from app.main import create_app
    from fastapi.testclient import TestClient

    class _Engine:
        model = "ok"

        async def answer(self, *, query, acl_scope, mode=None, **kwargs):
            from answer.models import Answer, AnswerStatus, Confidence, ConfidenceLevel

            return Answer(
                answer="I cannot answer this question based on the provided documents.",
                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                reason_code=ReasonCode.EVIDENCE_MISSING.value,
                reason=error_detail(ReasonCode.EVIDENCE_MISSING)["message"],
                confidence=Confidence(level=ConfidenceLevel.LOW, basis="nothing retrieved"),
                trace_id="trace-empty-1",
            )

    monkeypatch.setattr(router, "build_answer_engine", lambda **_: _Engine())
    app = create_app()
    app.dependency_overrides[get_principal] = lambda: _principal()
    client = TestClient(app)
    response = client.post(f"/v1/workspaces/{WS_ID}/ask", json={"query": "nothing here"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "insufficient_evidence"
    assert response.json()["reason_code"] == "EVIDENCE_MISSING"  # canonical


# --- 8. hydration decorator must not shadow the honest outcome ---------------


async def test_hydrating_orchestrator_keeps_the_unavailable_verdict() -> None:
    """The T-V1-022 decorator delegates ``retrieve``; the verdict still surfaces."""
    from answer.hydration import CanonicalHydrator, HydratingOrchestrator

    inner = await _orchestrator(lexical=_DownLexicalStore())
    decorated = HydratingOrchestrator(inner, CanonicalHydrator(None))
    items = await decorated.retrieve(query="hybrid retrieval", acl_scope=SCOPE)
    outcome = pop_retrieval_outcome()
    assert items == []
    assert outcome is not None
    assert outcome.status is RetrievalStatus.UNAVAILABLE
    assert outcome.reason_code is ReasonCode.RETRIEVAL_UNAVAILABLE


# --- 8b. ingress -> queue -> worker -> stores: the tracking ID crosses all ---


async def test_worker_binds_the_job_id_as_the_tracking_id(monkeypatch) -> None:
    """The queue payload (job id) becomes the correlation id for the whole job.

    That single binding is what joins the upload that enqueued the job to every
    worker stage and store write, with no schema change.
    """
    import jobs.ingest_worker as worker
    from core.logging import request_id_ctx

    observed: dict = {}

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Factory:
        def __call__(self):
            return _Session()

    async def _fake_process_job(session, job_id):
        observed["job_id"] = job_id
        observed["trace_id"] = request_id_ctx.get()

    monkeypatch.setattr(worker, "get_session_factory", lambda: _Factory())
    monkeypatch.setattr(worker, "process_job", _fake_process_job)

    job_id = uuid.uuid4()
    await worker.process_one(job_id)
    assert observed["job_id"] == job_id
    assert observed["trace_id"] == str(job_id)


async def test_worker_failure_record_carries_the_tracking_id(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The terminal failure log joins the same trace as the job poll."""
    import jobs.ingest_worker as worker
    from core.logging import reset_request_id, set_request_id
    from jobs.ingest_pipeline import IngestPipelineError

    job_id = uuid.uuid4()

    class _Job:
        id = job_id
        attempts = 1
        max_attempts = 1
        error_code = None
        error_message = None
        status = None
        stage = None
        finished_at = None

    class _Session:
        async def get(self, model, looked_up):
            return _Job() if looked_up == job_id else None

        async def commit(self):
            return None

    token = set_request_id(str(job_id))
    try:
        with caplog.at_level("WARNING"):
            await worker.record_failure(
                _Session(),
                job_id,
                IngestPipelineError("INTERNAL_ERROR", "boom", retryable=False),
            )
    finally:
        reset_request_id(token)

    failed = [record for record in caplog.records if record.message == "ingest_job_failed"]
    assert failed, f"expected the terminal failure log line, got: {caplog.text}"
    assert failed[-1].trace_id == str(job_id)
    assert failed[-1].error == "INTERNAL_ERROR"


# --- 9. the taxonomy, not a parallel one -------------------------------------


def test_every_leg_reason_code_resolves_through_the_shared_registry() -> None:
    from core.errors import ERROR_REGISTRY
    from retrieval.orchestrator import LEG_REASON_CODES

    for leg, code in LEG_REASON_CODES.items():
        assert code in ERROR_REGISTRY, f"{leg}: {code} not in the shared taxonomy"
    assert set(LEG_REASON_CODES) == {"embedding", "dense", "sparse", "lexical", "graph", "rerank"}


def test_required_legs_are_a_subset_of_the_declared_legs() -> None:
    from retrieval.orchestrator import LEG_REASON_CODES

    for mode, legs in REQUIRED_LEGS.items():
        assert set(legs) <= set(LEG_REASON_CODES), f"{mode}: unknown required leg {legs}"
    assert REQUIRED_LEGS[RetrievalMode.GRAPH.value] == (), "graph never fails a request closed"


def test_search_filter_and_items_are_unaffected_by_status_fields() -> None:
    """The status is additive: ``RetrievedItem`` keeps its frozen shape."""
    item = RetrievedItem(content="c", score=1.0)
    assert set(item.model_dump()) == set(RetrievedItem.model_fields)
    assert SearchFilter(workspace_ids=["ws-a"]).workspace_ids == ["ws-a"]


def test_orchestrator_declares_no_reason_code_literal_of_its_own() -> None:
    """Single-registry contract: no parallel taxonomy in the retrieval layer."""
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "packages" / "retrieval" / "orchestrator.py"
    ).read_text(encoding="utf-8")
    # Codes are only ever *referenced* through core.errors, never invented.
    assert not re.search(r'reason_code\s*=\s*"[A-Z_]{4,}"', source)
    assert "from core.errors import" in source
