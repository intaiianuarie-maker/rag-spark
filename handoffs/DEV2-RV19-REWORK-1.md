# DEV2 rework handoff — RV19 graph lane (POLICE findings F1/F2/F3)

Date: 2026-10-19
Owner: dev2

## Fixes
- F1: tombstone_document now acquires BEGIN IMMEDIATE before any read; reads/cleanup/tombstone insert inside transaction.
- F2: document->chunk mapping persisted by upsert_entities, upsert_relationships, and upsert_extraction; MemoryGraphStore updated similarly; exact chunk removal on deletion; fallback heuristic for unmapped rows.
- F3: replacement communities reactivated when members live; retired-chunk fence retained.

## Verification
- Ruff clean on packages/graph.
- Graph tests: 29 passed + 1 Postgres env-only failure (test_purge_uses_store_purger_seam).
- Probes (provenance/stale/replacement) pass.

## Notes
- No commits (museR stages).
