# DEV2 handoff — Super RAG review graph-lifecycle lane (T-RV19-03/05/06)

Date: 2026-10-19
Owner: dev2
Files: packages/graph/sqlite_store.py (and schema).

## Completed
- T-RV19-03 [P1]: Acquire BEGIN IMMEDIATE before reading tombstone/enable state in upsert_extraction. Fence now atomic with concurrent tombstone_document.
- T-RV19-05 [P2]: Persisted document->chunks association (graph_document_chunks table). tombstone_document now removes exact contributing chunk UUIDs instead of prefix heuristic; chunks retired into graph_retired_chunks.
- T-RV19-06 [P2]: Fence late extraction by retired chunk IDs (graph_retired_chunks). upsert_extraction refuses if any incoming chunk is retired; allows replacement-version provenance.

## Verification
- probes provenance: remaining chunks correct, deleted chunk absent from search.
- probes stale_version: absent_after True, stale_counts 0, old chunk not returned.
- Graph tests: 22 passed (1 environment-only Postgres failure: test_purge_uses_store_purger_seam).

## Notes
- No commits made (museR commits).
- Allowed files respected; no workflow_engine or docker-compose changes.
