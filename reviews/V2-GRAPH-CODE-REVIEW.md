# V2 graph code review — REQUEST CHANGES

Reviewed 2026-10-09 against HEAD `f49303ee9813ddc40177e445a67d1499abae4a7a` on `feat/v2-graph`, including the uncommitted graph files and tests identified in the handoff. Approval applies to no code at this stage; nothing was merged.

## Findings

1. **P1 — Deleted facts survive in partially pruned community summaries.** `packages/graph/sqlite_store.py:355-364` updates member IDs but retains the original title/summary whenever an entity survives. With Alice/d1 and Bob/d2 supporting `summary="Alice and Bob"`, deleting d1 leaves global search for Alice returning that summary with only Bob's c2 as evidence. This violates delete propagation and attaches deleted claims to unrelated surviving evidence. Tombstone affected summaries until rebuilt from surviving sources, or regenerate them with validated provenance. Add a regression asserting global retrieval cannot return deleted names/facts after partial community deletion, including edge-only invalidation. Reproduced with the SQLite store's database operations executed synchronously to isolate content behavior from sandbox thread scheduling.

2. **P2 — The anti-hallucination guard accepts invented full names and partial words.** `packages/graph/adapters/lightrag_extractor.py:119-123` accepts any normalized substring and otherwise only requires the first token. `_name_in_source("Alice InventedSurname", "Alice works here.")` and `_name_in_source("Ann", "Annual report.")` both return True. `_extract_one` consequently accepts these entities as source-backed. Require the full normalized token sequence with token boundaries; canonical aliases should require explicit validated mappings. Cover shared-first-name and substring negatives, not just entirely absent names.

3. **P2 — Flag-off parity has no baseline comparison.** `tests/unit/test_graph_retrieval_eval.py:334-350` checks only absence of graph scores and even passes for an empty result. It cannot substantiate byte-identical v1 behavior or catch changes to ranking, content, metadata, or other score fields. Compare serialized results from an independent v1/default HYBRID baseline and graph=None with HYBRID_GRAPH on identical fixtures. Normalize only nondeterministic fields and assert a nonempty baseline; cover reranking and degradation where relevant. Until then, withdraw the verified byte-identical claim.

4. **P2 — The claimed recall improvement compares unequal corpora.** `tests/unit/test_graph_retrieval_eval.py:111-144` deliberately omits ch-globex from both baseline indexes while including it in the graph. Recall 1.0 versus 0.667 measures missing input rather than retrieval quality. It is a useful synthetic fusion test, but does not complete G5's paired Q8 evaluation. Index identical source chunks for both conditions, use paired questions/relevance labels, report EvidenceRecall@20 and grounded-success deltas, and keep G5 pending until that evidence exists.

## Verification

- `/root/ko-venv/bin/python -m pytest -q tests/unit/test_graph_extractor.py tests/unit/test_graph_store_sqlite.py tests/unit/test_graph_retrieval_eval.py tests/unit/test_orchestrator.py tests/unit/test_answer_engine.py tests/unit/test_graph.py`: **79 passed in 1.52s**, outside sandbox. Initial sandbox run stalled at SQLite/asyncio thread completion; 74 non-SQLite tests passed independently in the sandbox. The stall is not a code finding.
- `/root/ko-venv/bin/ruff check packages/graph packages/retrieval/orchestrator.py tests/unit/test_graph_extractor.py tests/unit/test_graph_store_sqlite.py tests/unit/test_graph_retrieval_eval.py`: passed.
- `/root/ko-venv/bin/mypy packages/graph packages/retrieval/orchestrator.py`: passed, 10 source files.
- Focused content reproductions: both invented-name cases accepted; deleted Alice still searchable with evidence `['c2']`.
- Full integration/acceptance probes were not run. Existing test success does not cover the findings above.

## Scope and next handoff

G4 remains incomplete: the mode/API/UI/workspace toggle and factory wiring are not implemented. `GRAPH_MODE`/`GRAPH_DB_PATH` additions are documentation/config placeholders rather than active runtime switches. Keep the branch floating under V2-GRAPH-PLAN sections 3–4 until v1 Q9 and coordinator approval; do not mark G1–G5 all complete. Neo4j is optional later, not a required blocker.

Address the findings within graph/tests/docs ownership, add meaningful regression coverage, and return an exact patch/commit plus commands/results for re-review. Do not modify answer/ingestion/core or merge anything. The docker-compose change belongs to v1 and was excluded from this review. Reuse/notice edits are in the sibling spec repository, not in the app's changed-file list; identify that separate diff in the next handoff.
