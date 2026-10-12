# G4 — Graph API / Factory / UI integration contract (V2)

> Status: **contract prepared, integration NOT applied.** The protected
> `packages/answer/*`, `packages/core/*`, and product API/UI wiring are gated on
> accepted v1 Q9 and an explicit coordinator dispatch (V2-GRAPH-PLAN §3 seam
> contract; AGENTS.md §2). This document is the *exact, review-ready* change set
> so integration is mechanical once the gate opens. Nothing here is applied to
> the tree yet.
>
> Author: dev2 (Stream B) · 2026-10-10 · branch `feat/v2-graph`.

The graph leg itself (G1 extract, G2 store, G3 retrieve/fuse) is implemented,
tested, and reviewed-approved. What remains for "product completion" is the
thin wiring that exposes it behind the flag. This contract pins that wiring.

## 0. Current state (verified 2026-10-10)

| Seam | Today | Gap |
|---|---|---|
| `HybridRetrievalOrchestrator(graph=...)` | ✅ accepts an optional `graph` object; `GRAPH`/`HYBRID_GRAPH` modes; `TRACE_KEYS` includes `"graph"` | none — reviewed/approved |
| `packages/graph/factory.py::create_graph_retriever` | ✅ builds `GraphRetriever \| None` from `GRAPH_MODE`/`GRAPH_DB_PATH` | none — new this cycle, tested |
| `Search` endpoint mode | ✅ `SearchRequest.mode: RetrievalMode` already accepts `hybrid_graph`/`graph` | none at request-parsing level |
| `answer.factory.create_orchestrator()` | ⚠ builds dense+lexical+rerank, passes **no** `graph=` | must pass `graph=create_graph_retriever()` |
| `/ask` endpoint | ⚠ `AskRequest` has **no** `mode`; `engine.answer()` always calls `retrieve()` with default `HYBRID` | add opt-in `mode` |
| UI (search/chat) | ⚠ no graph mode toggle | add `mode` selector (frontend stream) |

`GRAPH_MODE=off` (the default) is the byte-identical v1 **retrieval** path: the
factory returns `None`, the orchestrator's graph leg no-ops, and no graph
retrieval/extraction runs. Note the import itself is **not** avoided — the
proposed `answer.factory` still does `from graph.factory import
create_graph_retriever` even when off (a cheap module import with no side
effects, no I/O, no service client); it simply constructs no `GraphRetriever`.
Byte-identical *output* (not zero imports) is what is locked, by
`test_graph_off_is_byte_identical_v1` and `test_graph_factory.py`. If a future
requirement is to avoid even the import, gate the import behind the flag rather
than claiming no graph code loads.

## 1. Protected change set (apply only after Q9 + coordinator dispatch)

### 1a. `packages/answer/factory.py` — wire the retriever (backend)

Replace `create_orchestrator` so it threads the optional graph retriever. The
change is additive and flag-gated; when `GRAPH_MODE=off` the argument is `None`
and behavior is unchanged.

```python
def create_orchestrator(*, collection: str = DEFAULT_COLLECTION):
    from ingestion.persistence import get_session_factory
    from retrieval.adapters.infinity_embed import InfinityEmbeddingProvider
    from retrieval.adapters.infinity_rerank import InfinityReranker
    from retrieval.adapters.pg_fts import PostgresLexicalStore
    from retrieval.adapters.qdrant_store import QdrantVectorStore
    from retrieval.orchestrator import HybridRetrievalOrchestrator
    from graph.factory import create_graph_retriever   # NEW (in-seam helper)

    dense = QdrantVectorStore(collection=collection)
    lexical = PostgresLexicalStore(session_factory=get_session_factory())
    embedding = InfinityEmbeddingProvider()
    reranker = InfinityReranker()
    return HybridRetrievalOrchestrator(
        dense=dense,
        embedding=embedding,
        lexical=lexical,
        collection=collection,
        reranker=reranker,
        graph=create_graph_retriever(),   # NEW: None when GRAPH_MODE=off
    )
```

Acceptance: `GRAPH_MODE=off` → orchestrator has `graph is None`; existing
answer/search tests unchanged. `GRAPH_MODE=rule` → `graph` is a `GraphRetriever`
over the embedded SQLite store at `GRAPH_DB_PATH`.

### 1b. `packages/answer/engine.py` + `apps/api/app/ask/router.py` — opt-in mode

The `/ask` surface should allow a caller to select a retrieval mode, defaulting
to `HYBRID` so existing clients are unaffected.

- `AskRequest` (ask router): add `mode: RetrievalMode = RetrievalMode.HYBRID`.
- `AnswerEngine.answer(...)`: add `mode: RetrievalMode = RetrievalMode.HYBRID`
  and pass it to `self.orchestrator.retrieve(mode=mode, ...)`.
- Router maps `body.model`/`body.mode` onto the engine call exactly as it
  already maps `model`.

Acceptance: default `/ask` (no `mode`) is byte-identical to today.
`mode=hybrid_graph` fuses the graph leg; `mode=graph` is graph-only. Both remain
ACL-before-ranking and citation-authentic (graph items carry `chunk_id`
provenance; the citation verifier drops any that don't resolve).

### 1c. UI — mode selector (frontend stream, code2)

Add a retrieval-mode control to the search and chat surfaces exposing
`keyword | semantic | hybrid | graph | hybrid_graph`, defaulting to `hybrid`.
Surface the `graph` trace leg in the debugger (nodes traversed, per-leg scores)
via the existing `scores["graph"]` on fused items. UI-only; no backend logic
change.

## 2. Non-negotiables (carried from V2-GRAPH-PLAN §1/§3)

- **ACL before ranking (R-004):** graph nodes/edges are workspace-scoped and the
  orchestrator re-filters every leg defensively before fusion. Locked by
  `test_graph_acl_scoped_no_cross_workspace_leak`.
- **Citations stay real — authoritative original-chunk hydration required
  (see §2a):** graph-derived `RetrievedItem`s currently carry a `chunk_id` **and
  a synthesized `[Knowledge Graph Context]` passage**, not the original chunk
  text. A chunk ID alone is **not** sufficient evidence of authentic displayed
  source content. Before a graph-derived answer is grounded, the original chunk
  must be re-hydrated from the authoritative store and ACL/version/provenance
  verified, with an exact source span located. See §2a for the full contract and
  the integration test that will prove it.
- **Flag-off parity:** `GRAPH_MODE=off` is byte-identical to v1
  (`test_graph_off_is_byte_identical_v1`).
- **No vendor leak:** graph backend stays behind `GraphStore` (SQLite default;
  Neo4j only via a future `packages/graph/adapters/` entry). Extractor stays
  behind `GraphExtractor` (LightRAG *pattern*, MIT, no source copied).
- **Do not fork the error contract:** extraction/retrieval errors use the shared
  Q0 `reason_code` taxonomy (imported, not redefined).

## 2a. Graph-citation hydration & provenance (MANDATORY before grounding)

The graph leg (`GraphRetriever.to_retrieved_items`) emits, per evidence chunk, a
`RetrievedItem` whose `content` is a **synthesized** `[Knowledge Graph Context]`
passage (entities/relationships summary) and whose `chunk_id` points at the
source. The v1 citation verifier (`answer.verification`) currently trusts items
already present in the retrieved map and grounds against whatever `content` it
was handed. Therefore, for graph-derived answers, a `chunk_id` alone is **not**
sufficient evidence of authentic displayed source content. Before any
graph-derived claim is accepted as grounded/cited, the integration MUST:

1. **Authoritative original-chunk hydration.** Resolve each graph citation's
   `chunk_id` to the **original chunk text** from the authoritative document
   store (not the synthesized graph passage), for the exact
   `document_id` / `document_version_id`.
2. **ACL / version / provenance checks.** Confirm the hydrated chunk is inside
   the caller's workspace scope (R-004), matches the cited document version, and
   carries real provenance (document, page, section). Drop the citation on any
   mismatch.
3. **Exact source span.** Locate the claim text within the hydrated original
   chunk (the `ExtractiveGrounder.locate` span) and surface `(start, end)` in the
   `Citation.span`. A citation with no locatable span in the original chunk is
   dropped.
4. **No synthesized content as evidence.** The `[Knowledge Graph Context]`
   passage may be shown as *context* but must never itself be the grounded
   source for a citation.

**Future integration test (to be added under the protected `answer`/API seam
after Q9; not in this graph-only cycle):**
`tests/integration/test_graph_citation_hydration.py` — assert that (a) a graph
citation to a real chunk hydrates the original text and grounds; (b) a graph
citation whose `chunk_id` does not resolve in the caller's scope is dropped; (c)
a claim is never grounded against the synthesized `[Knowledge Graph Context]`
passage; (d) an out-of-version `document_version_id` is rejected. Until that
test exists and is green, graph-derived answers must abstain
(`INSUFFICIENT_EVIDENCE`) rather than cite synthesized content.

## 3. Known limitation to disclose (not a blocker)

The fused orchestrator path currently **dilutes** the graph advantage: the graph
leg emits one `RetrievedItem` per evidence chunk with a uniform score, so RRF
cannot prioritize the multi-hop answer chunk over near-miss keyword hits, and
the default `context_top_k=6` window can truncate it. See
[`G5-EVAL-REPORT.md`](./G5-EVAL-REPORT.md) for the measured, honest result and
the retriever-level vs fused-level distinction. Fixing fusion scoring for graph
items is a candidate follow-up *after* Q9; it is out of scope for the flag-on
wiring above and must not be bundled into the protected integration.

## 4. Verification (to run after integration)

```bash
cd "/root/code/Super Rag/knowledge-os"
PYTHONPATH="packages:apps/api:apps/worker:.:tests" \
  APP_ENV=test MIGRATIONS_ON_BOOT=false LOG_JSON=false \
  /root/ko-venv/bin/python3 -m pytest tests/unit/test_graph_factory.py \
    tests/unit/test_orchestrator.py tests/unit/test_answer_engine.py -q
# Expected: all green; flag-off parity holds; no ACL/citation regressions.
```

Plus the ruff/mypy gates on `packages/graph/` and any touched protected file.
