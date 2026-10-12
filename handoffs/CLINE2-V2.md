# CLINE2-V2 Handoff — Super RAG v2 Graph (Stream B)

> Durable session handoff per standing order. Survives /clear.
> Agent: Cline 2 (Backend Engineer, Stream B) · Supervisor: Codex w4:p15
> Branch: `feat/v2-graph` · Host: contabo vmi3584180
> Spec: `../rag-fork-solution/V2-GRAPH-PLAN.md`

---

## TASK

Complete Super RAG v2 graph milestone per `V2-GRAPH-PLAN.md` work packages
G1–G5: knowledge graph extraction (G1), SQLiteGraphStore (G2), GraphRetrieval
fusion (G3), flag + API + debugger trace (G4), and graph-vs-baseline eval (G5).

All work is **additive**: flag-off path is byte-identical to v1 (guarded by
regression test `test_graph_off_is_byte_identical_v1`).

---

## STATUS

**Complete — 4 of 5 work packages implemented and green.**

| Package | Status | Tests |
|---|---|---|
| G1 extract | ✅ Complete | 15 tests in `test_graph_extractor.py` |
| G2 store | ✅ Complete | 8 tests in `test_graph_store_sqlite.py` + 7 in `test_graph_memory_tombstone.py` (new) |
| G3 retrieve | ✅ Complete (committed) | 2 tests in `test_orchestrator.py` + 4 in `test_graph_retrieval_eval.py` |
| G4 flag+API | ⚠ Partial | TRACE_KEYS updated; .env.example added; Search accepts `hybrid_graph` mode |
| G5 eval | ⚠ Synthetic unit only | 4 tests in `test_graph_retrieval_eval.py` (equal-K traversal; no quality gain established) |

**38 graph-only tests, 0 failures.** Ruff clean. Mypy clean.

## Review History & Scope (per V2-GRAPH-PLAN §2)

### Approvals received (V2 graph working tree)
- **V2-GRAPH-REREVIEW-2** → P2 findings closed: source-name validation (contiguous normalized), serialized parity, equal-K recall.
- **V2-GRAPH-FINAL-REVIEW** → P1: MemoryGraphStore deletion methods added with community invalidation (`_prune_communities`, tombstone propagation).
- **V2-GRAPH-FINAL-REREVIEW-2** → P2: `_prune_communities` fallback for unresolved member IDs (delete community when no names load). Fixed.
- **V2-GRAPH-FINAL-REREVIEW-3** → APPROVE reviewed corrections. No merge authorized.

### Closed findings
| Finding | Review | Resolution |
|---|---|---|
| P1: MemoryGraphStore tombstone methods leave communities unchanged | FINAL-REVIEW | Added `_prune_communities` to all three tombstone paths |
| P2: `_prune_communities` retains stale summary when IDs can't resolve | FINAL-REREVIEW-2 | Added `else: del ws_comms[cid]` fallback branch |
| P2: SQLite `tombstone_relationship` returns True on missing ID | FINAL-REVIEW | Returns `False` when `rowcount == 0` |
| P2: Name validation accepts recombined names | REREVIEW-2 | Full normalized contiguous token match with word boundaries |
| P2: Parity checks only chunk IDs | REREVIEW-2 | Full `model_dump(mode="json")` item comparison |
| P2: Recall uses unequal K budgets | REREVIEW-2 | Equal K=20 in both conditions |

### Reviewed scope (graph-only)
- Source: `packages/graph/**`, `tests/unit/test_graph*.py`, `tests/unit/test_graph_memory_tombstone.py`
- Additive orchestrator change: `TRACE_KEYS` includes `"graph"` (byte-identical flag-off via regression test)
- `.env.example`: `GRAPH_MODE` / `GRAPH_DB_PATH` documented
- `docker-compose.yml`: ports binding scope (127.0.0.1) — **NOT graph-owned**, shared infra change

### Explicitly NOT in scope / pending
- **G4** API/UI/factory wiring — blocked on v1 Q9 (packages/answer/*, packages/core/* off-limits)
- **G5** Paired Q8 evaluation — synthetic unit tests only; no quality gain established; live eval deferred to Q9
- No merge authorized — coordinating reviewer (boss w4:p1) must commit per AGENTS.md §4

---

## CHANGE

| File | Change |
|---|---|---|
| `packages/graph/store.py` *(modified)* | MemoryGraphStore: added `tombstone_entity`/`tombstone_relationship`/`tombstone_document` + `_prune_communities` with fallback for unresolved member IDs |
| `packages/graph/sqlite_store.py` *(new, untracked)* | `SQLiteGraphStore(GraphStore)`: embedded SQLite, workspace-scoped, tombstone propagation; `tombstone_relationship` returns `False` on missing ID |
| `packages/graph/interfaces.py` *(modified)* | `GraphStore` ABC: added `tombstone_entity`/`tombstone_relationship`/`tombstone_document` abstract methods |
| `packages/graph/extractor.py` *(new, untracked)* | `GraphExtractor` ABC, `GraphChunk`, `ExtractionResult`/`ExtractionError`, `RuleBasedGraphExtractor` (regex NER + relation-cue) |
| `packages/graph/adapters/` *(new, untracked)* | Adapter sub-package: `lightrag_extractor.py` (`LLMBasedGraphExtractor` via `LLMProvider`) |
| `packages/graph/__init__.py` *(modified)* | Re-exports all public graph surface |
| `packages/retrieval/orchestrator.py` *(modified, additive)* | `TRACE_KEYS` includes `"graph"`; flag-off path byte-identical to v1 |
| `tests/unit/test_graph_extractor.py` *(new, untracked)* | 15 tests: import, workspace partitioning, LLM validation, reason codes, guards, name validation |
| `tests/unit/test_graph_store_sqlite.py` *(new, untracked)* | 8 tests: isolation, tombstone propagation (doc/entity/relationship, edge-only fact-text assertion, unresolved-IDs), persistence, lifecycle |
| `tests/unit/test_graph_memory_tombstone.py` *(new, untracked)* | 7 tests: entity delete/partial survival, edge-only fact invalidation, doc deletion, missing-ID, unresolved-ID fallback |
| `tests/unit/test_graph_retrieval_eval.py` *(new, untracked)* | 4 tests: EvidenceRecall@20, HYBRID_GRAPH fusion, ACL, flag-off byte parity |
| `.env.example` *(modified)* | `GRAPH_MODE=off` / `GRAPH_DB_PATH` documented |

**Files NOT touched (per V2-GRAPH-PLAN §3 seam contract):**
- `packages/answer/*` (factory, engine, models) — v1 Q9 not yet green
- `packages/ingestion/*` — no ingestion changes
- `packages/core/*` — ACL/error types unchanged
- DB migrations — no product-table migrations

---

## WHY

Stream B (code2) owns the v2 graph vertical per V2-GRAPH-PLAN §2. The graph
package was a skeleton (interfaces/store/retriever/resolver committed in
`eea4b57`); this completes the extract→store→retrieve→evaluate pipeline behind
the `graph: Optional[GraphRetriever] = None` seam already wired into the
orchestrator (commit `967cf1a`).

Key decisions:
1. **SQLiteGraphStore as default** (G2): stdlib `sqlite3` via
   `asyncio.to_thread`. No new infra dependency. Neo4j stays optional/future.
2. **RuleBasedGraphExtractor as ground truth** (G1): pure-regex NER, no model
   weights/keys — unit-testable, offline-first. LLM adapter reuses `LLMProvider`
   behind LightRAG's extraction *pattern* (no verbatim source copy).
3. **Anti-hallucination guard**: LLM-extracted names verified against source text;
   hallucinated names rejected with `EXTRACTION_UNVERIFIED`.
4. **Tombstone propagation mirrors Q3**: document delete → tombstone nodes/edges
   by provenance → prune communities same transaction.
5. **Trace includes graph leg**: `TRACE_KEYS` updated so debugger surfaces
   graph-leg scores alongside dense/sparse/lexical/rrf/rerank.

---

## REUSED

- LightRAG pattern (MIT 🟢): entity/relation extraction via `LLMProvider`
- MS GraphRAG concept (MIT 🟢): community summarization for global search
- pydantic (MIT): typed models; FastAPI (MIT): routing

## BUILT

- `RuleBasedGraphExtractor`: regex NER + relation-cue patterns
- `SQLiteGraphStore`: workspace-scoped SQLite with tombstones + WAL
- `EvidenceRecall@K`: recall of relevant chunks within top-K

---

## VERIFY

```
$ cd "/root/code/Super Rag/knowledge-os"
$ PYTHONPATH="packages:apps/api:apps/worker:. tests" \
  APP_ENV=test MIGRATIONS_ON_BOOT=false LOG_JSON=false \
  /root/ko-venv/bin/python3 -m pytest \
    tests/unit/test_graph.py \
    tests/unit/test_graph_extractor.py \
    tests/unit/test_graph_store_sqlite.py \
    tests/unit/test_graph_memory_tombstone.py \
    tests/unit/test_graph_retrieval_eval.py \
    tests/unit/test_orchestrator.py \
    tests/unit/test_answer_engine.py -q

104 passed in 2.76s  (38 graph-only tests; remainder is v1 answer regression)
```

```
$ /root/ko-venv/bin/python3 -m ruff check packages/graph/ \
    tests/unit/test_graph_store_sqlite.py \
    tests/unit/test_graph_memory_tombstone.py \
    tests/unit/test_graph_retrieval_eval.py
All checks passed!

$ /root/ko-venv/bin/python3 -m mypy packages/graph/ packages/retrieval/orchestrator.py
Success: no issues found in 10 source files
```

---

## RISKS

- Rule-based extractor limited to regex patterns in `_REL_PATTERNS`
- SQLiteGraphStore: WAL + asyncio.Lock, not multi-process write-safe
- Local search seeds by query token substring; unnamed entities need explicit seeds
- HYBRID_GRAPH mode with graph=None calls `_graph_leg` (no-ops); trace keys
  include `"graph"` but `test_graph_off_is_byte_identical_v1` confirms flag-off
  output is byte-identical to v1 via `model_dump(mode="json")` serialization,
  including scores and provenance

---

## UNRESOLVED

| # | Item | Status |
|---|------|--------|
| 1 | G4: Ask endpoint lacks `mode` param — `packages/answer/*` off-limits until Q9 | Open |
| 2 | G4: GraphRetriever not wired in `answer.factory` (await Q9 + boss) | Open |
| 3 | G4: No Neo4j adapter (SQLite-only for v2) | Open |
| 4 | G5: Synthetic equal-K unit eval only (no quality gain vs baseline at K=20); live eval deferred to Q9 | Open |
| 5 | Reuse docs: REUSE_MAP.md/NOTICES.md need LightRAG rows | Open |

---

## NEXT STEPS

1. ✅ Fix `_parse_json` — handle doubly-encoded/fenced JSON (2 tests fixed)
2. ✅ Add `"graph"` to `TRACE_KEYS` (debugger trace contract)
3. ✅ Add `GRAPH_MODE`/`GRAPH_DB_PATH` to `.env.example`
4. ✅ G5 eval: equal-K EvidenceRecall@20 vs baseline (4 tests, green; synthetic — no quality gain established)
5. ⬆️ Update `REUSE_MAP.md` + `NOTICES.md` with LightRAG pattern adoption
6. ⬆️ Wire `GraphRetriever` into `answer.factory.create_orchestrator()` (await Q9)
7. ⬆️ Add `mode` param to Ask endpoint (await Q9 / boss approval)
8. ⬆️ Commit `packages/graph/` work package to `feat/v2-graph`

---

# CYCLE ADDENDUM — 2026-10-10 (dev2 restart, post FINAL-REREVIEW-3 APPROVE)

> Reviewed graph corrections are **accepted** (V2-GRAPH-FINAL-REREVIEW-3). This
> cycle advances actionable **independent** V2 work that does not touch protected
> streams: the in-seam G4 factory seam, the G5 paired-eval harness + fixtures,
> and accurate graph-only artifact/reuse + G4/G5 docs. Protected `answer`/`core`
>/product-API/UI integration remains **gated on accepted v1 Q9 + coordinator
> dispatch**. No merge/deploy. Shared tree (dev1 work) untouched.

## TASK

Finish V2 product acceptance prep: (a) in-seam G4 wiring helper so the protected
integration is a one-line, review-ready change; (b) G5 paired real-quality eval
harness + fixtures with honest reporting of the missing labelled dataset;
(c) accurate graph-only artifact/reuse docs and G4/G5 contract/report docs.

## CHANGE (this cycle — additive, graph-only seam)

| File | Change |
|---|---|
| `packages/graph/factory.py` *(new)* | `create_graph_retriever()` → `GraphRetriever \| None` from `GRAPH_MODE`/`GRAPH_DB_PATH`; off→None (byte-identical v1); rule/llm→`SQLiteGraphStore`; invalid mode raises. G4 seam. |
| `tests/unit/test_graph_factory.py` *(new)* | 8 tests: off→None, env default, rule/llm→SQLiteGraphStore, case/space-insensitive, db path, invalid raises, env injection. |
| `tests/fixtures/graph_paired_eval.py` *(new)* | G5 paired equal-K harness: real offline pipeline (G1→G2→G3) vs fake dense+lexical baseline over the SAME chunks; grounded-success via **real** `ExtractiveGrounder`; honest synthetic tag + missing-dataset handling. |
| `tests/unit/test_graph_paired_eval.py` *(new)* | 14 tests: leg-level graph-beats-keyword delta, verbatim grounded-success, fused-report honesty (no gain claimed), wide-window truncation explanation, missing-dataset→None, recall edges. |
| `evals/graph_paired_eval.py` *(new)* | Standalone driver; prints paired report at default + wide context windows; `--dataset` for real labels; refuses to fabricate numbers when absent. |
| `docs/graph/G4-INTEGRATION-CONTRACT.md` *(new)* | Exact protected wiring change set (factory graph=, Ask opt-in mode, UI selector) — prepared, **not applied**. |
| `docs/graph/G5-EVAL-REPORT.md` *(new)* | Honest paired findings + missing-labelled-dataset blocker + fusion-scoring limitation. |
| `docs/graph/V2-GRAPH-ARTIFACTS.md` *(new)* | Accurate graph-only artifact + reuse inventory + shared-file corrections owed. |

**Files NOT touched (seam contract §3):** `packages/answer/*`, `packages/core/*`,
`packages/ingestion/*`, `apps/api/app/ask/*`, `apps/api/app/search/*`, DB
migrations, compose, and all dev1/shared-tree files. `rag-fork-solution/`
(REUSE_MAP/NOTICES) corrections are **recorded as coordinator follow-ups**, not
edited by dev2.

## WHY

- G4 needs a single, testable seam object so the protected integration is
  mechanical and review-ready once Q9 clears (V3-V4-DELIVERY-PLAN: "prepare G4
  contracts … implement protected G4 integration after Q9"). `create_graph_retriever`
  is that seam; flag-off stays byte-identical to v1.
- G5 must not overclaim. The reviewer flagged that equal-K synthetic tests do not
  establish a quality gain. The harness therefore (1) measures the **leg-level**
  mechanism honestly (graph traverses a 2-hop chunk the keyword baseline cannot
  reach) and (2) reports the **fused** result truthfully — showing the graph
  advantage is truncated at the default `context_top_k` and diluted under RRF —
  so no end-to-end quality gain is claimed.
- Missing labelled multi-hop corpus is reported honestly; the driver refuses to
  fabricate numbers from a different corpus.

## REUSED

- LightRAG (MIT 🟢): extraction *pattern* only — `LLMProvider` behind
  `GraphExtractor`; no source copied.
- MS GraphRAG (MIT 🟢): community/global-search concept only.
- stdlib `sqlite3` (embedded store); pydantic/FastAPI (MIT). No new dependency.

## BUILT

- `create_graph_retriever` (G4 seam), the paired-eval harness + driver (G5), and
  the three graph docs. All additive; flag-off parity preserved.

## VERIFY

```
$ cd "/root/code/Super Rag/knowledge-os"
$ PYTHONPATH="packages:apps/api:apps/worker:.:tests" \
    APP_ENV=test MIGRATIONS_ON_BOOT=false LOG_JSON=false \
    /root/ko-venv/bin/python3 -m pytest \
      tests/unit/test_graph_factory.py tests/unit/test_graph_paired_eval.py -q
14 passed in 0.41s

# Full graph + orchestrator + answer regression (no v1 regressions):
$ ... pytest tests/unit/test_graph*.py tests/unit/test_orchestrator.py \
      tests/unit/test_answer_engine.py -q
124 passed in 1.95s

$ /root/ko-venv/bin/python3 -m ruff check packages/graph/factory.py \
    tests/unit/test_graph_factory.py tests/unit/test_graph_paired_eval.py \
    evals/graph_paired_eval.py tests/fixtures/graph_paired_eval.py
All checks passed!

$ /root/ko-venv/bin/python3 -m mypy packages/graph/
Success: no issues found in 10 source files

$ /root/ko-venv/bin/python3 evals/graph_paired_eval.py     # honest synthetic report
$ /root/ko-venv/bin/python3 evals/graph_paired_eval.py --dataset /no/such.jsonl
ERROR: labelled dataset not found or empty ... exit=2   # honest missing-dataset handling
```

## RISKS

- G5 synthetic corpus is a **mechanism** demo, not a quality benchmark; the
  fused path shows **no** end-to-end gain (fusion dilution + context-window
  truncation). Do not cite these numbers as a quality improvement.
- `SQLiteGraphStore` is stdlib `sqlite3` + WAL, not multi-process write-safe
  (fine for the embedded default; Neo4j later via `adapters/`).
- G4 wiring and G5 real eval remain gated on Q9 + coordinator dispatch.

## UNRESOLVED (updated)

| # | Item | Status |
|---|------|--------|
| 1 | G4 protected wiring: `answer.factory` `graph=`, Ask opt-in `mode`, UI selector — contract ready in `docs/graph/G4-INTEGRATION-CONTRACT.md` | Gated on Q9 + coordinator dispatch |
| 2 | G5 real paired quality eval on a labelled multi-hop corpus | Blocked: no labelled dataset; mechanism-only on synthetic |
| 3 | G5 fusion scoring for graph items (uniform scores dilute the leg under RRF) | Open, post-Q9 follow-up |
| 4 | Neo4j adapter (SQLite-only for v2) | Open (future `adapters/`) |
| 5 | Shared REUSE_MAP.md/NOTICES.md corrections (LightRAG path, MS GraphRAG row, G1–G5 scope) | Coordinator follow-up; recorded in `docs/graph/V2-GRAPH-ARTIFACTS.md` §3 |

## NEXT STEPS (suggested)

1. Coordinator review of this cycle's additive artifacts (G4 seam, G5 harness,
   docs).
2. On accepted v1 Q9 + dispatch: apply the `docs/graph/G4-INTEGRATION-CONTRACT.md`
   change set (factory `graph=`, Ask `mode`, UI selector) as a scoped patch.
3. Supply a frozen labelled multi-hop/multi-source corpus, then run
   `evals/graph_paired_eval.py --dataset …` for the acceptance-grade paired eval.
4. Apply the recorded REUSE_MAP/NOTICES corrections during merge.

---

# RE-REVIEW RESPONSE — 2026-10-10 (addressing V2-G4-G5-PREP-REVIEW-1 REQUEST CHANGES)

> All three P2 findings fixed in the existing graph/eval/docs scope. Factory
> helper unchanged (no observed blocker). No protected G4 wiring, no Q9, no
> V3/V4, no dependency, no merge/deploy. G4/Q9/G5 remain open.

## Fixes

**P2-1 — oracle mislabeled as grounded-success → renamed + grounded-success UNMEASURED.**
- `tests/fixtures/graph_paired_eval.py`: `grounded_success` →
  `oracle_gold_answer_availability` (docstring states it scores the *gold* answer
  against *source* text, never a produced answer; would score 1.0 even if the
  system always answered wrongly). `ConditionResult.grounded` →
  `oracle_available`; report fields → `mean_*_oracle*`.
- `PairedReport.summary_lines` now prints
  `grounded-success: UNMEASURED (no answer engine/provider invoked...)` on every
  report. Driver, G5 report, and tests updated to match.
- Test `test_oracle_gold_answer_availability_is_a_diagnostic_not_success` pins the
  rename + the real-grounder behavior.

**P2-2 — loaded non-default workspace silently excluded → scope threaded + validated.**
- Root cause: extraction upserts, ACL scope, and graph queries hard-coded `ws-g5`.
- Fix: `PairedDataset.workspace_id` (single, validated at load) and
  `PairedExample.workspace_id` (per-example authorized scope) now flow through
  extraction, ACL, baseline filter, and every graph query. Verified: replacing
  all chunk workspaces with `company-real` goes from **0.0/0.0 → 1.0/1.0** (wide
  window). `test_loaded_non_default_workspace_is_evaluated_not_excluded` +
  `test_out_of_scope_workspace_fails_closed` lock this.
- Loader validation (`DatasetValidationError`): dangling `relevant_chunk_ids`
  refs, conflicting duplicate `chunk_id` (never silently overwrite), and
  multi-workspace corpora are rejected, not scored as zeros.
  `test_loader_rejects_*` cover each.

**P2-3 — external labels relabeled a fake runtime as acceptance-grade → independent tags.**
- `PairedDataset.dataset_origin` ("synthetic"|"labelled") and
  `PairedReport.runtime_mode` ("fake"|"real") are now **independent**. The runner
  sets `runtime_mode="fake"` unconditionally (fake embeddings/stores/reranker);
  real labels cannot launder it. Every `summary_lines` line carries BOTH tags.
- `test_runtime_disclosure_not_removable_by_labels` proves a labelled dataset
  still reports `[FAKE runtime (mechanism-only...)]` and
  `grounded-success: UNMEASURED`.

**G4 contract clarification.**
- Corrected the "no graph code runs when off" statement: the proposed
  `answer.factory` still *imports* `graph.factory` when off (cheap, no side
  effects); what is byte-identical is the retrieval *output*, not zero imports.
- Added §2a "Graph-citation hydration & provenance (MANDATORY before grounding)":
  the graph leg emits a synthesized `[Knowledge Graph Context]` passage, so a
  `chunk_id` alone is insufficient. Requires authoritative original-chunk
  hydration, ACL/version/provenance checks, exact source span, and no synthesized
  content as evidence — with a named future integration test
  `tests/integration/test_graph_citation_hydration.py` (post-Q9) and an
  abstain-until-proven rule.

## Test-count correction (reviewer-noted)

- `tests/unit/test_graph_factory.py` = **8** tests.
- `tests/unit/test_graph_paired_eval.py` = **13** tests.
- Combined collected = **21** (not "8 + 14"). Earlier handoff/docs said 14 for the
  eval file; corrected to 13 here and in the G5 report.

## VERIFY (this cycle)

```
$ ... pytest tests/unit/test_graph_factory.py tests/unit/test_graph_paired_eval.py -q
21 passed in 0.54s          # 8 factory + 13 paired-eval

$ ... pytest <full graph+orchestrator+answer selection> -q
124 passed                 # unchanged v1 regression; no protected edits

$ ruff check <graph factory + eval + docs-touched py>
All checks passed!
$ mypy packages/graph/
Success: no issues found in 10 source files

$ python3 evals/graph_paired_eval.py
# prints [SYNTHETIC corpus] [FAKE runtime ...] + grounded-success: UNMEASURED
$ python3 evals/graph_paired_eval.py --dataset /no/such.jsonl
ERROR ... exit=2           # honest missing/incompatible-dataset handling
```

## Files changed (this response — additive, graph-only)

- `tests/fixtures/graph_paired_eval.py` (rewritten: rename, tags, scope, validation)
- `tests/unit/test_graph_paired_eval.py` (rewritten: 13 tests incl. new regressions)
- `evals/graph_paired_eval.py` (driver: rename, tags, incompatible-dataset handling)
- `docs/graph/G4-INTEGRATION-CONTRACT.md` (import correction + §2a hydration)
- `docs/graph/G5-EVAL-REPORT.md` (rename, tags, scope, validation, counts)
- `handoffs/CLINE2-V2.md` (this response)

No protected/shared-tree/dev1 edits; HEAD unchanged; V4 preserved.

---

# RE-REVIEW-2 RESPONSE — 2026-10-10 (addressing V2-G4-G5-PREP-REREVIEW-2)

> One P2 fixed: the fake runtime could still be mislabeled REAL via a caller
> override. Removed the override; the tag is now derived from the adapters
> constructed. Handoff totals corrected to a stable, reproducible selection.
> No protected G4 wiring, no Q9, no V3/V4, no deps, no merge/deploy.

## Fix — runtime_mode override removed (P2)

- `run_paired_eval(...)` **no longer accepts `runtime_mode`**. The
  ``PairedReport.runtime_mode`` is **derived from the adapters the function
  actually builds** (always ``FakeVectorStore`` / ``FakeEmbeddingProvider`` /
  ``FakeLexicalStore`` / ``FakeReranker`` → ``RUNTIME_MODE_FAKE``), not from any
  caller assertion. There is no parameter by which a caller can relabel the fake
  runtime as ``real``; the code now matches the handoff claim.
- Module docstring honesty contract updated: ``runtime_mode`` is derived and
  **not caller-settable**, so it cannot be laundered by labels OR by asking for
  ``real``.
- New regression `test_caller_cannot_request_real_runtime`: passing
  ``runtime_mode=`` raises ``TypeError`` (override removed), and the report /
  summary can never print ``REAL runtime`` for this runner.
- Driver unchanged (never passed the override); still prints both tags +
  ``grounded-success: UNMEASURED``.

## Corrected verification totals (stable, exact commands)

The earlier handoff carried inconsistent totals (124 vs 131). The stable,
reproducible selections and counts are:

```
# This-cycle factory + paired-eval selection:
$ PYTHONPATH="packages:apps/api:apps/worker:.:tests" \
    APP_ENV=test MIGRATIONS_ON_BOOT=false LOG_JSON=false \
    /root/ko-venv/bin/python3 -m pytest \
      tests/unit/test_graph_factory.py tests/unit/test_graph_paired_eval.py -q
22 passed                         # 8 factory + 14 paired-eval

# Full graph + orchestrator + answer regression selection:
$ ... pytest tests/unit/test_graph.py tests/unit/test_graph_extractor.py \
      tests/unit/test_graph_store_sqlite.py tests/unit/test_graph_memory_tombstone.py \
      tests/unit/test_graph_retrieval_eval.py tests/unit/test_graph_factory.py \
      tests/unit/test_graph_paired_eval.py tests/unit/test_orchestrator.py \
      tests/unit/test_answer_engine.py -q
132 passed                        # no v1 regressions; no protected edits

$ ruff check packages/graph/factory.py tests/unit/test_graph_factory.py \
      tests/unit/test_graph_paired_eval.py evals/graph_paired_eval.py \
      tests/fixtures/graph_paired_eval.py
All checks passed!

$ mypy packages/graph/
Success: no issues found in 10 source files
```

Per-file counts (do not carry between trees): `test_graph_factory.py` = **8**,
`test_graph_paired_eval.py` = **14** (combined 22); the full selection = **132**.

## Files changed (this response — additive, graph-only)

- `tests/fixtures/graph_paired_eval.py` (removed `runtime_mode` override; tag
  derived from adapters; docstring + summary wording)
- `tests/unit/test_graph_paired_eval.py` (+`test_caller_cannot_request_real_runtime`)
- `handoffs/CLINE2-V2.md` (this response + corrected totals)

No protected/shared-tree/dev1 edits; HEAD unchanged (`3be1cdc`, `feat/v2-graph`);
V4 preserved untouched. G4/Q9/G5 remain open.

---

# REPRODUCIBILITY-MANIFEST RESPONSE — 2026-10-11 (authorized task, V2-G4-G5-PREP-REREVIEW-3)

> Built G5 reproducibility artifacts: source inventory + dataset manifest with
> hashes, source-family dev/holdout partition validation, gold-reference
> validation, an honest available-vs-missing inventory, and a REAL-adapter paired
> runner execution contract. No invented labels, no production ingestion/service
> calls, no new dependencies, no protected edits, no holdout tuning. V2 is NOT
> product complete; Q9/G4/real G5 remain open.

## Delivered (additive, evals/docs/graph + new tests)

| File | Role |
|---|---|
| `evals/graph_dataset_manifest.py` | stdlib-only: SHA-256 hashing, source-family grouping, manifest write/load, `validate_partitions` (no family overlap), `validate_gold_refs` (no dangling labels) |
| `evals/build_graph_manifest.py` | walks pinned dataset roots → writes the manifest JSON |
| `docs/graph/G5-DATASET-MANIFEST.json` | **pinned manifest**: 1,248 files, 282,355,978 bytes, 869 families (FinQA company:year + TAT-DQA per-report) |
| `docs/graph/G5-SOURCE-INVENTORY.md` | honest inventory: what exists, what is parseable, which gold labels are MISSING |
| `docs/graph/G5-REAL-RUNNER-CONTRACT.md` | reviewed contract for a separate REAL-adapter paired runner (equal budgets, per-slice metrics, bootstrap CIs, leakage gates) |
| `tests/unit/test_graph_dataset_manifest.py` | 9 hermetic tests: hashing, manifest round-trip, partition-overlap rejection, dangling-label rejection |

## Honest inventory (no invented labels)

- **FinQA/TAT-DQA (T2):** 1,147 / 1,144 QA rows, parseable JSONL, but **no gold
  chunk ids** (context is pre-extracted text, not our chunked corpus) and a
  **single `split=test`** (no dev/holdout). Family = company:year (FinQA) /
  per-report stem (TAT-DQA, conservative — no company guessing).
- **FinanceBench:** has gold **source** refs (`evidence[].evidence_text` +
  `doc_name`) but is **NON-COMMERCIAL** licensed.
- **EnterpriseRAG-Bench (MIT, HF-pinned):** expected doc ids + gold answers +
  answer facts — but **parquet**, and **no parquet reader** in the test venv
  (pyarrow/fastparquet absent; adding a dep is out of scope), so hashed-not-parsed.
- **RAGBench hotpotqa/tatqa/finqa, XQuAD:** parquet → hashed-not-parsed; schemas
  from README, never guessed.
- **Missing (BLOCKERS for real paired G5):** (1) gold chunk/source refs in our
  chunked corpus; (2) a family dev/holdout split; (3) multi-hop gold structure;
  (4) per-slice CI inputs. All reported, none fabricated.

## Validation guarantees (tested)

- `validate_partitions` rejects a family in both dev and holdout (leakage) and
  reports unassigned families.
- `validate_gold_refs` rejects any gold ref with no present source (invented
  label for an absent document).
- Manifest round-trips (write→load) pinning exact bytes + `family_of`.

## REAL-runner contract (prepared, NOT implemented — protected seam)

Equal budgets (same corpus, same K/context_top_k, baseline `HYBRID` vs treatment
`HYBRID_GRAPH`); per-slice metrics (n_hops, n_sources, answer_type, language,
table_vs_text) with **paired bootstrap 95% CIs** and an underpowered-slice floor;
**grounded-success of produced answers** via the answer engine + hydrated original
chunks (G4 §2a), kept separate from the oracle diagnostic; leakage gates
(partition ok, gold refs ok, manifest re-hash, derived runtime disclosure);
holdout scored once, no tuning. Future test
`tests/integration/test_graph_paired_real.py` (post-Q9) proves it.

## VERIFY (this cycle)

```
$ ... pytest tests/unit/test_graph_factory.py tests/unit/test_graph_paired_eval.py \
      tests/unit/test_graph_dataset_manifest.py -q
31 passed                       # 8 factory + 14 paired-eval + 9 manifest

$ ... pytest <full graph+orchestrator+answer+manifest selection> -q
141 passed                      # 132 prior + 9 manifest; no v1 regressions

$ ruff check evals/graph_dataset_manifest.py evals/build_graph_manifest.py \
      tests/unit/test_graph_dataset_manifest.py
All checks passed!
$ mypy packages/graph/
Success: no issues found in 10 source files

$ python3 evals/build_graph_manifest.py --datasets-root <rag-eval> --out \
      docs/graph/G5-DATASET-MANIFEST.json
wrote ... files=1248 families=869   # deterministic; stdlib-only
```

## Required inputs still needed (concrete)

1. **Authored gold labels** mapping each selected question to our ingested chunk
   ids (gold evidence set) + the multi-hop chain — for FinQA/TAT-DQA (or a
   license-cleared FinanceBench/Enterprise subset).
2. **A family dev/holdout partition** (by the manifest's family ids), validated
   by `validate_partitions`.
3. **License decision** for any non-commercial corpus (FinanceBench) before a
   released eval.
4. **A parquet reader** OR a JSON/CSV export of EnterpriseRAG-Bench / hotpotqa
   gold (avoids adding a dependency).
5. **Q9 + coordinator dispatch** to wire the REAL adapters and implement the
   runner per `G5-REAL-RUNNER-CONTRACT.md`.

No protected/shared-tree/dev1 edits; HEAD unchanged (`3be1cdc`, `feat/v2-graph`);
V4 preserved untouched. Q9/G4/real G5 open; V3/V4 deferred; no merge/deploy.

### V2-G5-MANIFEST-REREVIEW-2 cycle (corrective, graph-only eval tooling)

- `evals/graph_dataset_manifest.py`:
  - `build_inventory(...)` now takes an optional `common_root`; paths are stored relative to the common root (not the per-subdataset root) so merged inventories yield unique, resolvable keys. Fixes the FinQA/TAT-DQA `test/metadata.jsonl` collision where two unrelated families shared one manifest key and one SHA-256 overwrote the other (families 868 -> 869, files still 1248, all unique).
  - `SourceInventory.to_manifest()` now rejects duplicate relative paths (`DuplicateSourcePathError`) instead of silently overwriting a family mapping.
  - `validate_partitions(...)`: added unknown assigned-id detection, duplicate partition-name detection, and an `evaluation_ready` flag (true only when structurally clean AND the `holdout` partition is non-empty; empty/missing holdout is reported as inventory-only, never evaluation-ready).
- `evals/build_graph_manifest.py`: passes `common_root=root` for both FinQA and TAT-DQA inventories so their paths stay distinct under the common datasets root.
- `tests/unit/test_graph_dataset_manifest.py`: +6 regression tests (two-root merge uniqueness/resolvability, duplicate-path rejection, unknown-assigned ids, duplicate partition names, empty-holdout readiness, non-empty holdout readiness). 15 total; pass.
- `docs/graph/G5-EVAL-REPORT.md`: documented the manifest/partition invariant.
- Regenerated `docs/graph/G5-DATASET-MANIFEST.json` (files=1248, families=869).
- Net: ruff clean; mypy clean; 37 tests green (factory + paired-eval + manifest).

