"""Ask-time multimodal leg: page-level visual hits -> canonical text chunks -> RRF.

The leg is optional and off by default. Every drop/degrade path is locked here
with fakes (no Qdrant, no Postgres, no network): collection missing, embed
failure, qdrant error, foreign workspace, tombstoned version, page without
canonical chunks, flag off, timeout and the ACL/document filter.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from core.auth import ACLScope
from retrieval import orchestrator as orch_mod
from retrieval.interfaces import RetrievalMode, RetrievedItem
from retrieval.multimodal_leg import MultimodalLeg, MultimodalNotRun
from retrieval.orchestrator import HybridRetrievalOrchestrator, RetrievalStatus

pytestmark = pytest.mark.unit

STAGES_OFF = ("embedding", "dense", "sparse", "lexical", "graph", "fusion", "rerank", "context")


class Embedder:
    model_name = "google/gemini-embedding-2"
    revision = "google/gemini-embedding-2"
    dimension = 2

    def __init__(self, fail=False, delay=0.0):
        self.fail, self.delay, self.queries = fail, delay, []

    async def embed_query(self, query):
        self.queries.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("openrouter 500 secret-host")
        return [1.0, 0.0]


class Store:
    def __init__(self, items=None):
        self.items = items or []

    async def search(self, **kwargs):
        return self.items


def _hit(version="v1", page=2, ws="A", doc="doc1", score=0.7, **extra):
    return RetrievedItem(
        content="[Image page=2 figure=none]",
        score=score,
        workspace_id=ws,
        document_id=doc,
        document_version_id=version,
        page=page,
        metadata={
            "media_type": "image/png",
            "visual_citation": {"page": page, "figure_id": None, "bbox": None},
            **extra,
        },
    )


class MMStore:
    def __init__(self, hits=(), exists=True, fail=False):
        self.hits, self.exists, self.fail = list(hits), exists, fail
        self.calls = []

    async def collection_exists(self, collection):
        return self.exists

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("qdrant down 10.0.0.5")
        return self.hits


class Pages:
    """Canonical page resolver (pg_fts.resolve_page_chunks seam)."""

    def __init__(self, chunks, fail=False):
        self.chunks = chunks  # {(version, page): [(chunk_id, workspace, doc, text, meta)]}
        self.fail, self.calls = fail, []

    async def __call__(self, *, pairs, query_filter):
        self.calls.append((list(pairs), list(query_filter.workspace_ids)))
        if self.fail:
            raise RuntimeError("db down")
        out = []
        for version, page in pairs:
            for cid, ws, doc, text, meta in self.chunks.get((version, page), []):
                if ws not in query_filter.workspace_ids:
                    continue
                out.append(
                    RetrievedItem(
                        chunk_id=cid,
                        workspace_id=ws,
                        document_id=doc,
                        document_version_id=version,
                        page=page,
                        content=text,
                        score=0.0,
                        metadata=dict(meta),
                    )
                )
        return out


PAGE2 = {
    ("v1", 2): [
        ("c1", "A", "doc1", "canonical page two text", {"filename": "f.pdf"}),
        ("c2", "A", "doc1", "second chunk", {}),
    ]
}


def _leg(store, pages, embedder=None, **kw):
    return MultimodalLeg(
        store=store,
        embedder=embedder if embedder is not None else Embedder(),
        resolve_pages=pages,
        collection="knowledge_multimodal",
        **kw,
    )


def _dense():
    return RetrievedItem(
        chunk_id="d0", document_id="docD", workspace_id="A", content="dense text", score=0.9
    )


def _orch(leg, dense=None, lexical=None):
    return HybridRetrievalOrchestrator(
        dense=Store(dense if dense is not None else [_dense()]),
        embedding=Embedder(),
        lexical=lexical or Store(),
        collection="fx",
        multimodal=leg,
    )


async def _run(orch, mode=RetrievalMode.HYBRID, scope=None):
    return await orch.retrieve_result(
        query="figure of revenue", acl_scope=scope or ACLScope(workspace_ids=["A"]), mode=mode
    )


# --- flag off: byte-identical -------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_has_no_multimodal_stage_and_same_items():
    off = await _run(_orch(None))
    assert tuple(off.stages) == STAGES_OFF
    assert "multimodal" not in json.dumps(off.stages)
    assert off.status is RetrievalStatus.OK
    assert [i.chunk_id for i in off.items] == ["d0"]
    assert set(off.items[0].scores) == {"dense", "rrf"}
    assert "extraction" not in off.items[0].metadata


# --- happy path -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_visual_hit_maps_to_canonical_page_chunks_and_fuses():
    store, pages, emb = MMStore([_hit()]), Pages(PAGE2), Embedder()
    result = await _run(_orch(_leg(store, pages, emb)))
    assert result.status is RetrievalStatus.OK
    st = result.stages["multimodal"]
    assert st["status"] == "completed" and st["candidate_count"] == 2
    assert st["returned_context_count"] == 2 and st["latency_ms"] >= 0
    by_id = {i.chunk_id: i for i in result.items}
    assert set(by_id) == {"d0", "c1", "c2"}
    c1 = by_id["c1"]
    assert c1.content == "canonical page two text"  # canonical text, never the image stub
    assert "Image page" not in c1.content
    assert set(c1.scores) == {"multimodal", "rrf"}  # no fabricated dense/lexical vote
    assert c1.metadata["retrieval_modality"] == "visual"
    assert c1.metadata["media_type"] == "image/png"
    assert c1.metadata["visual_citation"]["page"] == 2
    assert c1.metadata["filename"] == "f.pdf"
    assert emb.queries == ["figure of revenue"]
    assert pages.calls == [([("v1", 2)], ["A"])]


@pytest.mark.asyncio
async def test_visual_only_hit_is_model_derived_support_only():
    result = await _run(_orch(_leg(MMStore([_hit()]), Pages(PAGE2))))
    c1 = next(i for i in result.items if i.chunk_id == "c1")
    assert c1.metadata["extraction"] == "vision"
    assert c1.metadata["visual_only"] is True


def test_visual_only_label_equals_answer_vision_label():
    from answer.models import VISION_EXTRACTION

    assert orch_mod.VISUAL_ONLY_EXTRACTION == VISION_EXTRACTION


@pytest.mark.asyncio
async def test_text_corroborated_chunk_keeps_text_provenance_and_both_scores():
    text_hit = RetrievedItem(
        chunk_id="c1",
        document_id="doc1",
        workspace_id="A",
        content="canonical page two text",
        score=0.8,
    )
    result = await _run(_orch(_leg(MMStore([_hit()]), Pages(PAGE2)), dense=[text_hit]))
    c1 = next(i for i in result.items if i.chunk_id == "c1")
    assert set(c1.scores) == {"dense", "multimodal", "rrf"}
    assert "extraction" not in c1.metadata and "visual_only" not in c1.metadata
    c2 = next(i for i in result.items if i.chunk_id == "c2")
    assert c2.metadata["visual_only"] is True


@pytest.mark.asyncio
async def test_existing_vision_chunk_keeps_vision_label():
    pages = Pages(
        {
            ("v1", 2): [
                (
                    "c1",
                    "A",
                    "doc1",
                    "[Model-generated page description] x",
                    {"extraction": "vision"},
                )
            ]
        }
    )
    result = await _run(_orch(_leg(MMStore([_hit()]), pages)))
    assert next(i for i in result.items if i.chunk_id == "c1").metadata["extraction"] == "vision"


@pytest.mark.asyncio
async def test_hybrid_graph_mode_also_runs_leg():
    result = await _run(
        _orch(_leg(MMStore([_hit()]), Pages(PAGE2))), mode=RetrievalMode.HYBRID_GRAPH
    )
    assert result.stages["multimodal"]["status"] == "completed"


@pytest.mark.asyncio
async def test_keyword_mode_does_not_run_leg():
    emb = Embedder()
    result = await _run(
        _orch(_leg(MMStore([_hit()]), Pages(PAGE2), emb)), mode=RetrievalMode.KEYWORD
    )
    assert result.stages["multimodal"]["status"] == "not_run"
    assert emb.queries == []


# --- not_run with a reason ---------------------------------------------------------


@pytest.mark.asyncio
async def test_collection_missing_is_not_run_with_reason_and_status_ok():
    store = MMStore([_hit()], exists=False)
    result = await _run(_orch(_leg(store, Pages(PAGE2))))
    st = result.stages["multimodal"]
    assert st["status"] == "not_run" and st["reason"] == "collection_missing"
    assert st["candidate_count"] is None
    assert result.status is RetrievalStatus.OK
    assert store.calls == []  # never searched a missing collection
    assert [i.chunk_id for i in result.items] == ["d0"]


@pytest.mark.asyncio
async def test_no_api_key_is_not_run_and_never_embeds():
    leg = MultimodalLeg(
        store=MMStore([_hit()]),
        embedder=None,
        resolve_pages=Pages(PAGE2),
        collection="knowledge_multimodal",
    )
    result = await _run(_orch(leg))
    st = result.stages["multimodal"]
    assert st["status"] == "not_run" and st["reason"] == "no_api_key"
    assert result.status is RetrievalStatus.OK


# --- failed -> degraded -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leg_kwargs",
    [
        {"embedder": Embedder(fail=True)},
        {"store": MMStore([_hit()], fail=True)},
        {"pages": Pages(PAGE2, fail=True)},
    ],
)
async def test_failures_degrade_and_text_legs_still_answer(leg_kwargs):
    store = leg_kwargs.get("store", MMStore([_hit()]))
    pages = leg_kwargs.get("pages", Pages(PAGE2))
    result = await _run(_orch(_leg(store, pages, leg_kwargs.get("embedder"))))
    assert result.status is RetrievalStatus.DEGRADED
    assert result.stages["multimodal"]["status"] == "failed"
    assert result.stages["multimodal"]["candidate_count"] is None
    assert [f.leg for f in result.legs_failed] == ["multimodal"]
    assert [i.chunk_id for i in result.items] == ["d0"]
    blob = json.dumps(result.stages) + repr(result.legs_failed)
    assert "secret-host" not in blob and "10.0.0.5" not in blob  # class name only


@pytest.mark.asyncio
async def test_timeout_degrades(monkeypatch):
    monkeypatch.setattr(orch_mod, "MULTIMODAL_LEG_TIMEOUT_S", 0.01)
    result = await _run(_orch(_leg(MMStore([_hit()]), Pages(PAGE2), Embedder(delay=1.0))))
    assert result.status is RetrievalStatus.DEGRADED
    assert result.stages["multimodal"]["status"] == "failed"
    assert result.legs_failed[0].error_type == "TimeoutError"
    assert [i.chunk_id for i in result.items] == ["d0"]


# --- drops: never fabricate, never leak ------------------------------------------------


@pytest.mark.asyncio
async def test_page_with_no_canonical_chunks_is_dropped_not_invented():
    result = await _run(_orch(_leg(MMStore([_hit(page=9)]), Pages(PAGE2))))
    st = result.stages["multimodal"]
    assert st["status"] == "completed" and st["candidate_count"] == 0
    assert [i.chunk_id for i in result.items] == ["d0"]


@pytest.mark.asyncio
async def test_tombstoned_or_superseded_version_resolves_to_nothing():
    # the canonical resolver returns no rows for a dead version
    result = await _run(_orch(_leg(MMStore([_hit(version="vDEAD")]), Pages(PAGE2))))
    assert result.stages["multimodal"]["candidate_count"] == 0
    assert [i.chunk_id for i in result.items] == ["d0"]


@pytest.mark.asyncio
async def test_foreign_workspace_point_is_dropped_before_resolution():
    pages = Pages({("v1", 2): PAGE2[("v1", 2)], ("vB", 2): [("cB", "B", "docB", "foreign", {})]})
    hits = [_hit(version="vB", ws="B", doc="docB"), _hit()]
    result = await _run(_orch(_leg(MMStore(hits), pages)))
    assert "cB" not in {i.chunk_id for i in result.items}
    assert pages.calls == [([("v1", 2)], ["A"])]  # foreign page never even looked up


@pytest.mark.asyncio
async def test_resolver_row_from_other_workspace_or_document_is_dropped():
    rows = {
        ("v1", 2): [
            ("cX", "B", "doc1", "leaked", {}),
            ("cY", "A", "docOther", "wrong doc", {}),
            ("c1", "A", "doc1", "ok", {}),
        ]
    }

    class LeakyPages(Pages):  # ignores the filter, like a buggy resolver would
        async def __call__(self, *, pairs, query_filter):
            return [
                RetrievedItem(
                    chunk_id=c,
                    workspace_id=w,
                    document_id=d,
                    document_version_id="v1",
                    page=2,
                    content=t,
                    score=0.0,
                )
                for c, w, d, t, _ in rows[("v1", 2)]
            ]

    result = await _run(_orch(_leg(MMStore([_hit()]), LeakyPages({}))))
    assert {i.chunk_id for i in result.items} == {"d0", "c1"}


@pytest.mark.asyncio
async def test_hit_without_page_or_version_is_dropped():
    hits = [_hit(page=None), _hit(version=None)]
    hits[0].metadata.pop("visual_citation")
    pages = Pages(PAGE2)
    result = await _run(_orch(_leg(MMStore(hits), pages)))
    assert pages.calls == [] or pages.calls == [([], ["A"])]
    assert [i.chunk_id for i in result.items] == ["d0"]


# --- ACL / document filter -------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_is_searched_with_mandatory_workspace_and_document_filter():
    from retrieval.interfaces import SearchFilter

    store, pages = MMStore([_hit()]), Pages(PAGE2)
    orch = _orch(_leg(store, pages))
    await orch.retrieve_result(
        query="q",
        acl_scope=ACLScope(workspace_ids=["A"]),
        extra_filter=SearchFilter(document_ids=["doc1"], language="ro", metadata={"k": "v"}),
    )
    call = store.calls[0]
    assert call["collection"] == "knowledge_multimodal"
    f = call["query_filter"]
    assert f.workspace_ids == ["A"] and f.document_ids == ["doc1"]
    assert f.language is None and f.metadata == {}  # chunk-level filters go to the resolver
    assert call["query_identity"].model == "google/gemini-embedding-2"


@pytest.mark.asyncio
async def test_hit_outside_document_filter_is_dropped():
    from retrieval.interfaces import SearchFilter

    pages = Pages(PAGE2)
    orch = _orch(_leg(MMStore([_hit(doc="doc1")]), pages))
    result = await orch.retrieve_result(
        query="q",
        acl_scope=ACLScope(workspace_ids=["A"]),
        extra_filter=SearchFilter(document_ids=["other"]),
    )
    assert [i.chunk_id for i in result.items] == ["d0"]


@pytest.mark.asyncio
async def test_empty_scope_fails_closed_before_any_call():
    store, emb = MMStore([_hit()]), Embedder()
    result = await _orch(_leg(store, Pages(PAGE2), emb)).retrieve_result(
        query="q", acl_scope=ACLScope(workspace_ids=[])
    )
    assert result.items == [] and store.calls == [] and emb.queries == []


@pytest.mark.asyncio
async def test_leg_direct_not_run_error_carries_reason():
    leg = _leg(MMStore(exists=False), Pages({}))
    from retrieval.interfaces import SearchFilter

    with pytest.raises(MultimodalNotRun) as exc:
        await leg.search(query="q", query_filter=SearchFilter(workspace_ids=["A"]))
    assert exc.value.reason == "collection_missing"


# --- canonical page resolver (PostgresLexicalStore.resolve_page_chunks) ----------------

V1 = "11111111-1111-1111-1111-111111111111"


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, sink, rows):
        self.sink, self.rows = sink, rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params):
        self.sink.append((str(sql), params))
        return _Rows(self.rows)


def _pg(rows=()):
    from retrieval.adapters.pg_fts import PostgresLexicalStore

    sink: list = []
    return PostgresLexicalStore(session_factory=lambda: _Session(sink, list(rows))), sink


@pytest.mark.asyncio
async def test_resolve_page_chunks_is_canonical_scoped_and_lifecycle_gated():
    from retrieval.interfaces import SearchFilter

    store, sink = _pg(
        [
            {
                "chunk_id": "c1",
                "workspace_id": "A",
                "document_id": "doc1",
                "document_version_id": V1,
                "content": "t",
                "page": 2,
                "section": None,
                "metadata": {"k": 1},
            }
        ]
    )
    flt = SearchFilter(workspace_ids=["A"], document_ids=["doc1"], language="ro")
    out = await store.resolve_page_chunks(pairs=[(V1, 2), ("not-a-uuid", 3)], query_filter=flt)
    sql, params = sink[0]
    assert "FROM chunks c" in sql and "d.deleted_at IS NULL" in sql
    assert "dv.superseded_at IS NULL" in sql and "d.current_version_id" in sql
    assert "c.workspace_id::text = ANY(:wss)" in sql and "c.chunk_index" in sql
    assert params["vs"] == [V1] and params["pgs"] == [2]  # non-UUID version dropped
    assert params["wss"] == ["A"] and params["dids"] == ["doc1"] and params["lang"] == "ro"
    assert [(i.chunk_id, i.page, i.document_version_id) for i in out] == [("c1", 2, V1)]
    assert out[0].content == "t" and out[0].score == 0.0


@pytest.mark.asyncio
async def test_resolve_page_chunks_empty_inputs_never_query():
    from retrieval.interfaces import SearchFilter

    store, sink = _pg()
    assert (
        await store.resolve_page_chunks(pairs=[], query_filter=SearchFilter(workspace_ids=["A"]))
        == []
    )
    assert await store.resolve_page_chunks(pairs=[(V1, 1)], query_filter=SearchFilter()) == []
    assert (
        await store.resolve_page_chunks(
            pairs=[("x", 1)], query_filter=SearchFilter(workspace_ids=["A"])
        )
        == []
    )
    assert sink == []


# --- Qdrant store seam ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qdrant_collection_exists_wraps_client_and_errors():
    from retrieval.adapters.qdrant_store import QdrantVectorStore, VectorStoreError

    class Client:
        def __init__(self, result):
            self.result = result

        async def collection_exists(self, name):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    assert await QdrantVectorStore(client=Client(True)).collection_exists("c") is True
    assert await QdrantVectorStore(client=Client(False)).collection_exists("c") is False
    with pytest.raises(VectorStoreError):
        await QdrantVectorStore(client=Client(RuntimeError("down"))).collection_exists("c")


# --- factory wiring (flag / key / sensitive) --------------------------------------------


def _factory(monkeypatch, **env):
    import answer.factory as answer_factory
    import retrieval.adapters.pg_fts as fts_mod
    import retrieval.adapters.qdrant_store as qdrant_mod

    class _Qdrant:
        def __init__(self, **kwargs):
            self.collection = kwargs.get("collection")

    class _Lexical:
        def __init__(self, **kwargs):
            pass

        async def resolve_page_chunks(self, **kwargs):
            return []

    monkeypatch.setattr(qdrant_mod, "QdrantVectorStore", _Qdrant)
    monkeypatch.setattr(fts_mod, "PostgresLexicalStore", _Lexical)
    monkeypatch.setattr("ingestion.persistence.get_session_factory", lambda: object())
    monkeypatch.setenv("GRAPH_MODE", "off")
    for name in ("MULTIMODAL_EMBED_ENABLED", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return answer_factory


def test_factory_flag_off_builds_no_leg_and_passes_no_kwarg(monkeypatch):
    factory = _factory(monkeypatch, OPENROUTER_API_KEY="k")
    assert factory.create_orchestrator().multimodal is None


@pytest.mark.parametrize("flag", ["true", "1", " On "])
def test_factory_flag_on_with_key_wires_leg_with_shared_embedder(monkeypatch, flag):
    factory = _factory(monkeypatch, MULTIMODAL_EMBED_ENABLED=flag, OPENROUTER_API_KEY="k")
    orch = factory.create_orchestrator()
    assert orch.multimodal.embedder is orch.embedding  # same model, same provider
    assert orch.multimodal.collection == "knowledge_multimodal"
    assert orch.multimodal.store.collection == "knowledge_multimodal"


def test_factory_flag_on_without_key_wires_leg_that_reports_no_api_key(monkeypatch):
    factory = _factory(monkeypatch, MULTIMODAL_EMBED_ENABLED="true")
    assert factory.create_orchestrator().multimodal.embedder is None


def test_factory_sensitive_workspace_never_gets_leg(monkeypatch):
    factory = _factory(monkeypatch, MULTIMODAL_EMBED_ENABLED="true", OPENROUTER_API_KEY="k")
    assert factory.create_orchestrator(sensitive=True).multimodal is None


# --- Stage 11 interplay: a visual-only chunk is never standalone evidence -----------------


@pytest.mark.asyncio
async def test_stage11_rejects_claim_backed_only_by_visual_only_chunk():
    from answer.models import Claim
    from answer.verification import ExtractiveGrounder, verify_claims

    result = await _run(_orch(_leg(MMStore([_hit()]), Pages(PAGE2))))
    items = [i for i in result.items if i.chunk_id in {"c1", "c2"}]
    claim = Claim(text="canonical page two text", chunk_ids=["c1"])
    verified, citations = await verify_claims(
        [claim], items, allowed_workspace_ids={"A"}, claim_grounder=ExtractiveGrounder()
    )
    assert verified == [] and citations == []  # vision-only support does not verify

    # the same text surfaced by a text leg stays ordinary, verifiable evidence
    text_hit = RetrievedItem(
        chunk_id="c1",
        document_id="doc1",
        workspace_id="A",
        content="canonical page two text",
        score=0.8,
    )
    both = await _run(_orch(_leg(MMStore([_hit()]), Pages(PAGE2)), dense=[text_hit]))
    verified, citations = await verify_claims(
        [claim], both.items, allowed_workspace_ids={"A"}, claim_grounder=ExtractiveGrounder()
    )
    assert [c.chunk_ids for c in verified] == [["c1"]]
    assert citations[0].extraction is None
