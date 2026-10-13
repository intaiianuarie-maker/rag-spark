# G4 — Graph API / Factory / UI integration contract (V2)

> Status: **backend wiring APPLIED at HEAD; UI selector remains.** The
> `answer.factory` `graph=create_graph_retriever()`, `AskRequest.mode` /
> `AnswerEngine.answer(mode)` (HYBRID default), and `SearchRequest.mode`
> passthrough are merged (5dcb6c1; canonical hydration + ingest graph hook in
> 46fbf67, G2-APPROVED). The V2-GRAPH-PLAN §3 gate was satisfied via those
> post-Q9 exception merges (DECISIONS exception note pending — coordinator
> owns it). True remaining gap: UI mode selector in `apps/web` (no
> graph/hybrid_graph toggle; trace-panel label covered by D2).
>
> **Answer-path behaviour has since moved on and §2/§2a below now describe
> what the code does, not a plan.** T-V1-022 landed canonical hydration before
> context build *and* verify, T-V1-047 collapsed its N+1 into one bulk store
> pass (`ChunkLookup.get_many`), and T-V1-052 put lost-region disclosure on
> **every** `answer()` return path. The three "must" statements that used to
> sit in §2/§2a as unmet requirements are now enforced, and the test they
> promised exists and is green. The UI-selector gap is unchanged and still the
> only blocker named here.
>
> Author: dev2 (Stream B) · 2026-10-10 · updated 2026-10-12 (code2 docs-only) ·
> updated 2026-10-13 (code2, T-V1-048: hydration + disclosure synced to HEAD).

The graph leg itself (G1 extract, G2 store, G3 retrieve/fuse) is implemented,
tested, and reviewed-approved. The backend exposure is also merged (factory
graph wiring, ask/search mode passthrough, hydration + ingest hook — see §0).
What remains for "product completion" is the UI selector. This contract pins
that wiring (applied parts marked ✅, remaining parts marked ⚠).

## 0. Current state (wiring verified 2026-10-10; answer-path rows 2026-10-13)

| Seam | Today | Gap |
|---|---|---|
| `HybridRetrievalOrchestrator(graph=...)` | ✅ accepts an optional `graph` object; `GRAPH`/`HYBRID_GRAPH` modes; `TRACE_KEYS` includes `"graph"` | none — reviewed/approved |
| `packages/graph/factory.py::create_graph_retriever` | ✅ builds `GraphRetriever \| None` from `GRAPH_MODE`/`GRAPH_DB_PATH` | none — new this cycle, tested |
| `Search` endpoint mode | ✅ `SearchRequest.mode: RetrievalMode` already accepts `hybrid_graph`/`graph` | none at request-parsing level |
| `answer.factory.create_orchestrator()` | ✅ APPLIED (5dcb6c1): passes `graph=create_graph_retriever()` (None when `GRAPH_MODE=off`) | none — flag-off parity holds |
| `/ask` endpoint | ✅ APPLIED (5dcb6c1): `AskRequest.mode` + `AnswerEngine.answer(mode)`, HYBRID default | none backend-side |
| UI (search/chat) | ⚠ no graph mode toggle | add `mode` selector (frontend stream) — TRUE REMAINING GAP |
| `answer.hydration::CanonicalHydrator` | ✅ T-V1-022/T-V1-047: bulk-hydrates every candidate ref to canonical text **before** context build and **before** verify; one store pass per `hydrate()` via `ChunkLookup.get_many` | none — §2a is enforced, not aspirational |
| `answer.engine` disclosure | ✅ T-V1-052: all **7** `answer()` returns funnel through `_finalize` (lost-region disclosure + optional-leg degradation) | none — undisclosing return paths: 0 |

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

## 1. Protected change set (backend subset APPLIED via 5dcb6c1/46fbf67; UI pending)

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
- **Citations stay real — authoritative original-chunk hydration (see §2a):
  ENFORCED, not aspirational.** Graph-derived `RetrievedItem`s still carry a
  `chunk_id` **and a synthesized `[Knowledge Graph Context]` passage**, and a
  chunk ID alone is still not evidence of authentic source content. What
  changed is that this no longer depends on reviewer vigilance:
  `answer.hydration.CanonicalHydrator` replaces each ref's text with the
  authoritative original **before** the prompt is built and **before**
  verification runs, so the synthesized passage can never become a citation.
  §2a states what the code does today and which test pins it.
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
source. That part is unchanged — and a `chunk_id` alone is still **not**
sufficient evidence of authentic displayed source content.

What is no longer true is the old framing of this section: the v1 citation
verifier no longer "trusts items already present in the retrieved map and
grounds against whatever `content` it was handed". T-V1-022 inserted
`answer.hydration.CanonicalHydrator` **upstream** of both consumers, so the
four rules below are properties of the pipeline rather than obligations on
each caller:

1. **Authoritative original-chunk hydration.** ✅ `CanonicalHydrator.hydrate()`
   resolves every candidate ref to the original chunk text for its exact
   `document_id` / `document_version_id`, from the authoritative store — never
   the synthesized graph passage. `HydratingOrchestrator` applies it at the
   retrieval seam, so hydration **precedes** context build; `verify_claims`
   bulk-hydrates the cited refs again (fresh, bypassing the cache) so
   verification sees current store state.
2. **ACL / version / provenance checks.** ✅ Two layers, both fail-closed. The
   **store** refuses a row whose `documents.deleted_at` or
   `document_versions.superseded_at` is set, and drops a ref that is not
   UUID-shaped (it can never name a row). The **hydrator** then re-applies the
   caller scope on every read — including cache hits — so a chunk outside
   `allowed_workspace_ids` is never returned, and a shared hydrator cannot leak
   one caller's chunk to another. Unresolvable or foreign refs are simply
   absent, and the caller treats absence as rejection.
3. **Exact source span.** ✅ `ExtractiveGrounder` locates the claim text within
   the hydrated original and surfaces `(start, end)` as `Citation.span`; a
   claim that is not a verbatim substring of the canonical text is not
   grounded.
4. **No synthesized content as evidence.** ✅ Enforced by ordering, not by a
   filter: canonical text replaces the retrieved payload *before* the prompt is
   built, and graph descriptions are tagged `routing_hint` / `is_graph` so they
   decide *which* chunks are candidates but can never be cited themselves.

**One store pass.** T-V1-047 added `ChunkLookup.get_many` (declared on the
`ChunkLookup` protocol, implemented by both `PostgresChunkLookup` and
`MemoryChunkLookup`). `hydrate()` collects the refs it still has to read and
resolves them in a **single** query — the ~16 single-row reads per `/ask` it
used to issue are one `IN (…)` statement — with per-id semantics identical to
`get`. `CanonicalHydrator.lookups` still counts *refs read* (the T-V1-022
metric), so its value is unchanged; only the query count collapsed.

**The test that proves it exists and is green:**
`tests/unit/test_graph_citation_hydration.py` (5 tests, verified passing at
HEAD). Note the path: it lives under `tests/unit/`, **not**
`tests/integration/` as an earlier revision of this document claimed.

## 2b. Lost-region disclosure on the answer path (T-V1-052)

Related to §2a but a separate guarantee, and worth its own section because it
is the reason a partially-read source can never masquerade as a complete one —
including in a graph-assisted answer whose evidence came from such a source.

The ingestion pipeline parks a job `needs_review` when part of a source could
not be read (image-only PDF pages, an unexpandable table) and writes the lost
regions onto every chunk it publishes. Retrieval hands that metadata back on
the items, and the answer layer now says the same thing the job said.

**Every return path discloses.** `AnswerEngine.answer()` has **7** return
paths — 2 standing (`ANSWERED`, `PARTIAL`), 4 abstentions (`PARSE_FALLBACK`,
`EVIDENCE_MISSING`, `NO_CLAIMS`, `CITATION_INVALID`) and the fail-closed
`UNAVAILABLE` leg-down return. All 7 funnel through a single `_finalize()`
helper, so **undisclosing return paths: 0**. Before T-V1-052 the abstentions
bypassed the disclosure entirely: an answer that declined to claim anything
over a partially-read source said nothing about the pages nobody read.

Two rules, both hard:

- **A standing answer is labelled `needs_review`** and lists the regions that
  were never read, with confidence **capped at `MEDIUM`** — `HIGH` claims
  completeness, and completeness is exactly what a partially-read source does
  not have.
- **An abstention keeps its own `reason_code`** (it is already not a claim to
  have read anything) **and still discloses**: the limitation is appended, the
  status stays `INSUFFICIENT_EVIDENCE`.

`_finalize()` applies `_disclose_source` first and the optional-leg
`_disclose` (T-V1-026 `DEGRADED`) second. When both apply, the degradation
owns the single status field, but the lost-region **limitation is already
appended and the confidence already capped**, so the user reads both facts.

The precise boundary is `parse_lost_regions` (pinned on both sides by
`jobs.ingest_pipeline.PARSE_LOST_REGIONS_KEY` and an engine source-inspection
test), so a chunk indexed before that metadata existed simply does not
disclose — correct, since nothing was known to be lost.

## 3. Known limitation to disclose (not a blocker)

*Not the disclosure of §2b* — this is a scoring limitation, unrelated to
lost-region or provenance disclosure, and none of T-V1-022/047/052 touched
fusion, so it stands as written.

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
