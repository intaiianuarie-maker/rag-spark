# V2 Graph — artifact & reuse inventory (Stream B, G1–G5)

> Accurate, graph-only inventory of what dev2 owns on `feat/v2-graph`, its reuse
> provenance, and the exact shared-file corrections still owed. Additive only:
> the flag-off path is byte-identical to v1.
>
> Author: dev2 (Stream B) · 2026-10-10 · branch `feat/v2-graph`.

## 1. Owned artifacts (graph-only seam)

### Production code (`packages/graph/`)

| File | Role | Reuse / origin |
|---|---|---|
| `interfaces.py` | `Entity`/`Relationship`/`GraphCommunity`/`GraphSearchResult`; `GraphStore` ABC (workspace-scoped, tombstone lifecycle) | BUILT (custom core glue) |
| `extractor.py` | `GraphExtractor` ABC, `GraphChunk`, `ExtractionResult`/`ExtractionError`, `RuleBasedGraphExtractor` (regex NER + relation cues) | BUILT; LightRAG *pattern* only |
| `adapters/lightrag_extractor.py` | `LLMBasedGraphExtractor` via `LLMProvider` (LiteLLM); JSON-schema prompt; `_parse_json` fenced/double-encoded robustness | LightRAG pattern (MIT), no source copied |
| `sqlite_store.py` | `SQLiteGraphStore` — stdlib `sqlite3`, WAL, workspace-partitioned, tombstone + community pruning | BUILT (embedded default; Neo4j later) |
| `store.py` | `MemoryGraphStore` — in-memory reference impl for tests | BUILT |
| `resolver.py` | `EntityResolver`/`SimpleEntityResolver` — alias/name canonicalization | BUILT |
| `retriever.py` | `GraphRetriever` — local (1/2-hop) + global (community) search → `RetrievedItem`s with chunk provenance | BUILT; MS GraphRAG community concept (MIT) |
| `factory.py` | `create_graph_retriever()` — builds `GraphRetriever \| None` from `GRAPH_MODE`/`GRAPH_DB_PATH`; **G4 seam** (new this cycle) | BUILT |

### Additive orchestrator change (`packages/retrieval/orchestrator.py`)

- `graph: Any | None = None` constructor seam; `GRAPH`/`HYBRID_GRAPH` modes;
  `_graph_leg`; `TRACE_KEYS += "graph"`. Flag-off path byte-identical to v1
  (locked by `test_graph_off_is_byte_identical_v1`). Reviewed/approved.

### Config (`.env.example`)

- `GRAPH_MODE=off|rule|llm`, `GRAPH_DB_PATH` (default `./.graph/graph.db`).

### Tests (`tests/unit/`)

| File | Tests | Covers |
|---|---|---|
| `test_graph.py` | — | interfaces/resolver baseline |
| `test_graph_extractor.py` | 15 | G1: workspace partitioning, LLM validation, name anti-hallucination guard, reason codes |
| `test_graph_store_sqlite.py` | 8 | G2: isolation, tombstone propagation, persistence, lifecycle |
| `test_graph_memory_tombstone.py` | 7 | G2: community invalidation on entity/edge/doc delete, unresolved-ID fallback |
| `test_graph_retrieval_eval.py` | 4 | G3: multi-hop traversal, HYBRID_GRAPH fusion, ACL scoping, flag-off parity |
| `test_graph_factory.py` | 8 | G4 seam: off→None, rule/llm→SQLiteGraphStore, invalid mode raises, env injection |
| `test_graph_paired_eval.py` | 14 | G5: paired leg-level delta, real-grounder grounded-success, fused honesty, missing-dataset handling |
| `test_orchestrator.py` | — | v1 hybrid regression incl. graph trace key |

### Eval harness (G5)

| File | Role |
|---|---|
| `tests/fixtures/graph_paired_eval.py` | paired equal-K evaluator (real offline pipeline + real grounder) |
| `evals/graph_paired_eval.py` | standalone driver; honest missing-dataset handling |

### Docs (this cycle)

| File | Role |
|---|---|
| `docs/graph/G4-INTEGRATION-CONTRACT.md` | exact protected wiring change set (post-Q9) |
| `docs/graph/G5-EVAL-REPORT.md` | honest paired-eval findings + dataset blocker |
| `docs/graph/V2-GRAPH-ARTIFACTS.md` | this inventory |

## 2. Reuse provenance (licenses)

| Upstream | License | Used as | Where | Verbatim source? |
|---|---|---|---|---|
| LightRAG | MIT 🟢 | extraction *pattern* (per-chunk typed nodes/edges + provenance; JSON robustness) behind `GraphExtractor` | `extractor.py`, `adapters/lightrag_extractor.py` | **No** — pattern only |
| MS GraphRAG | MIT 🟢 | community-level summarization concept for global search | `interfaces.py` (`GraphCommunity`), `retriever.py` (`global_search`) | **No** — concept only |
| LiteLLM (via `LLMProvider`) | MIT (avoid enterprise/) | LLM extraction calls behind the `GraphExtractor`/`LLMProvider` interface | `adapters/lightrag_extractor.py` | **No** — interface only, no SDK type crosses |
| stdlib `sqlite3` | PSF | embedded graph store (G2) | `sqlite_store.py` | n/a |
| pydantic / FastAPI | MIT | typed models, routing | throughout | library use |

No copyleft, no `ee/`/`enterprise/`, no vendor SDK leaked past an adapter.

## 3. Corrections owed in SHARED files (coordinator action — not edited by dev2)

These live in `../rag-fork-solution/` (outside this repo's git tree, shared with
dev1). dev2 does **not** edit them directly; they are recorded here for the
coordinator to apply during review/merge.

1. **`rag-fork-solution/NOTICES.md`** — LightRAG row path is stale:
   `packages/graph/extractors/` → correct paths are `packages/graph/extractor.py`
   and `packages/graph/adapters/lightrag_extractor.py`. Add an **MS GraphRAG**
   row (MIT, concept) currently missing from NOTICES. Add a **G4 factory** note
   (`packages/graph/factory.py`).
2. **`rag-fork-solution/REUSE_MAP.md`** — "Adopted (v2 graph, G1–G3)" should
   extend to **G1–G5** and reference `packages/graph/factory.py` (G4) plus the
   G5 harness (`tests/fixtures/graph_paired_eval.py`,
   `evals/graph_paired_eval.py`). Keep the LightRAG/MS GraphRAG license verdicts
   unchanged (both MIT, pattern/concept only, no source copied).
3. Both docs should state the SQLite default and that Neo4j remains a future
   `packages/graph/adapters/` entry (no dependency added now).

## 4. Explicitly NOT owned / pending (gate status)

- **G4 protected wiring** (`packages/answer/*`, `packages/core/*`, product
  API/UI): gated on accepted v1 Q9 + coordinator dispatch. Contract ready in
  `G4-INTEGRATION-CONTRACT.md`; **not applied**.
- **G5 real paired quality eval**: blocked on a missing labelled multi-hop
  corpus. Mechanism demonstrated on synthetic data only — see
  `G5-EVAL-REPORT.md`. No quality gain claimed.
- **Neo4j adapter**: not built (SQLite-only for v2).
- **Fusion scoring for graph items**: known limitation, post-Q9 follow-up.
- **No merge/deploy** authorized; only the coordinator (`w4:p17`) merges.
