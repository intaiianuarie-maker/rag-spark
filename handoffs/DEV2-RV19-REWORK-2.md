# DEV2 rework-2 handoff — RV19 F5/F6

Date: 2026-10-19
Owner: dev2

## Fixes
- F5: upsert_entities and upsert_relationships now wrapped in BEGIN IMMEDIATE (read->merge->write atomic); prevents lost updates on concurrent merges.
- F6: tombstone_chunks recomputes document contributions; tombstones rows left with no live supporting chunk; mixed retirement+deletion regression passes.

## Verification
- Ruff clean on packages/graph.
- Graph tests: 29 passed + 1 Postgres env-only failure.
- Union-preservation and mixed retirement+deletion probes pass.

## Notes
- No commits (museR stages).

## Rework-3 (regression tests)
Added permanent regression tests for F1-F6 in test_graph_store_sqlite.py, test_graph_store_concurrency.py, test_graph_memory_tombstone.py. Ruff clean on lane (extractor.py pre-existing). pytest 35 passed + 1 Postgres env-only.

## Rework-4 (test teeth)
Added forced cross-process regressions F1/F4/F5 plus kept 4 teeth-bearing tests. Ruff clean on lane (extractor.py pre-existing). pytest 38 passed + 1 PG env.
