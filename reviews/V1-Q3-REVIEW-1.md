# V1-Q3 Review 1 — APPROVED

- Reviewer: coordinator (`w4:p19`, Antigravity)
- Agent: `dev1` (`w4:p12`)
- Scope: `handoffs/CLINE1-V1-Q3-R1.md`, `packages/ingestion/dedup.py`, `apps/api/app/documents/delete.py`, `apps/api/app/documents/reads.py`, `tests/integration/test_dedup_versions.py`, `tests/integration/test_security_placeholders.py`, `tests/unit/test_workspace_policy.py`
- Contract: Product Gate Q3 (Tombstone Lifecycle)

## Verdict: APPROVED

1. **Purge & Tombstone Lifecycle:**
   - `build_purge_filter(workspace_id, document_id)` provides an exact, unit-tested `SearchFilter` with `workspace_ids=[str(workspace_id)]` and `document_ids=[str(document_id)]`.
   - `purge_document` properly orders operations: PostgreSQL chunk purge -> `document.deleted_at` committed -> store purgers (`_purge_fts` and `_purge_qdrant`) executed with honest failure reporting and logging (`store_purge_failed`).
   - `reads.py` and delete routes return honest 404 (`document_not_found`) upon querying tombstoned documents.

2. **Unretrievability & Isolation:**
   - Both live integration tests and unit tests confirm that upon document deletion:
     - PostgreSQL chunks table count is 0.
     - PostgreSQL lexical FTS count is 0.
     - Qdrant vector store point count is 0.
     - Keyword and hybrid searches return 0 hits for the deleted document content.
     - Concurrent/live control documents in the same workspace remain intact.
     - Ask pipeline does not cite deleted documents.

3. **Verification Results:**
   - `pytest tests/integration/test_dedup_versions.py tests/integration/test_security_placeholders.py tests/unit/test_workspace_policy.py -q`: **41 passed, 2 xfailed** (the 2 out-of-scope placeholder tests remain marked xfail; `test_deleted_document_is_tombstoned_and_never_retrieved` unskipped and fully passing).
   - `ruff check packages/ingestion/dedup.py apps/api/app/documents/delete.py apps/api/app/documents/reads.py tests/integration/test_dedup_versions.py tests/integration/test_security_placeholders.py`: **clean**.
   - `mypy packages/ingestion apps/api/app/documents`: **clean** across 25 source files.
