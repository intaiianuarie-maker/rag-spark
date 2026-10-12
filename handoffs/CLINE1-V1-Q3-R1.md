# CLINE1 — V1-Q3-TOMBSTONE-LIFECYCLE R1 (submitted for review)

Scope: `V1-Q3-TOMBSTONE-LIFECYCLE.md` only (prerequisite: Q1 finite-embeddings
approved in `reviews/V1-Q1-REVIEW-2.md`). No commit, merge, deploy, or gate
closure claimed. Coordinator owns acceptance.

## Changed files (allowed only)

- `packages/ingestion/dedup.py` (SHA `51c8cf81…`)
  - New `build_purge_filter(workspace_id, document_id) -> SearchFilter` carrying
    exactly `workspace_ids=[str(workspace_id)]` + `document_ids=[str(document_id)]`;
    both default store purgers (`_purge_fts`, `_purge_qdrant`) use it.
  - `purge_document` store-purger failures now log `store_purge_failed`
    (store/ws/doc/error) and report `False` per store — never swallowed as success.
  - No change to tombstone/commit ordering: chunks deleted → `deleted_at` set →
    commit → store purges → per-store report.
- `apps/api/app/documents/delete.py` (SHA `069078a4…`): docstring now describes
  the V1-Q3 contract (tombstone + document-scoped FTS/Qdrant purge, honest 404
  on reads, unretrievable in search/ask). No route logic changed.
- `apps/api/app/documents/reads.py`: unchanged (SHA `72e5788f…`; already honest —
  `_get_live_document` 404s `document_not_found` when `deleted_at` is set; list
  routes filter `deleted_at.is_(None)`).
- `packages/retrieval/adapters/qdrant_store.py`, `pg_fts.py`: unchanged — verified
  `delete` executes honestly (Qdrant raises `VectorStoreError` on failure, returns
  pre-delete scroll count; FTS returns real `rowcount`, `0` only for empty scope).
- `tests/integration/test_dedup_versions.py` (SHA `a9d672d6…`, +103):
  `test_delete_purges_fts_and_qdrant_and_is_unretrievable` — two-document
  lifecycle (doomed + live control): pre-delete rows/points/search present;
  post-delete chunks=0, `lexical_docs`=0, Qdrant points=0, keyword search zero
  hits, detail 404; live control untouched (rows/points/search intact).
- `tests/integration/test_security_placeholders.py` (SHA `c853ede2…`): xfail
  **removed** from `test_deleted_document_is_tombstoned_and_never_retrieved`;
  now a real lifecycle test (upload→index→delete→404 + zero keyword hits + ask
  never cites via stubbed-LLM-over-real-post-delete-retrieval check). Other two
  placeholders keep their xfail (out of Q3 scope).
- `tests/unit/test_workspace_policy.py` (SHA `204e8c20…`, +123, service-free):
  `build_purge_filter` shape test, `purge_document` fan-out test (both purgers
  called, report all-True, tombstone set), failure-report test (False, no false
  success). Fake session (no live DB).

Diff stat (allowed files): 5 files, 440 insertions, 32 deletions.

## Before / after behavior

- Before: purge already flowed (`purge_document` → FTS + Qdrant deletes with the
  right filter — confirmed by live E2E probe: pre-delete chunks=1/FTS=1/Qdrant=1
  + search hit; post-delete 0/0/0 + zero hits + detail 404 + report
  `{postgres, fts, qdrant: True}`), but store failures were silent (`except:
  report=False` with no log), the filter shape had no unit pin, the only purge
  test covered Postgres chunks, and the security placeholder asserted wrong
  routes (`/v1/documents`, `/v1/search`) under xfail.
- After: failures are logged + honestly reported; filter shape is unit-pinned;
  live integration proves document-scoped purge (control doc survives); the
  placeholder is a real passing test on real routes; ask verified to never cite
  the deleted doc (stubbed LLM claims a dead chunk → verification strips it).

## Verification (exact spec order, from repo root; Python 3.14.4)

1. `/root/ko-venv/bin/python3 -m py_compile packages/ingestion/dedup.py
   apps/api/app/documents/delete.py apps/api/app/documents/reads.py
   tests/integration/test_dedup_versions.py
   tests/integration/test_security_placeholders.py` → `PY_COMPILE_OK`
2. `/root/ko-venv/bin/pytest tests/integration/test_dedup_versions.py -q`
   → `8 passed in 14.63s` (7 pre-existing + 1 new Q3 purge test)
3. `/root/ko-venv/bin/pytest tests/integration/test_security_placeholders.py -q`
   → `1 passed, 2 xfailed in 6.25s` (tombstone test now a real PASS, no
   xfail/skip; other two placeholders still xfail — out of scope)
4. `/root/ko-venv/bin/ruff check packages/ingestion/dedup.py
   apps/api/app/documents/delete.py apps/api/app/documents/reads.py
   tests/integration/test_dedup_versions.py
   tests/integration/test_security_placeholders.py`
   → `All checks passed!` (two interim I001 import-sort findings fixed)
5. `/root/ko-venv/bin/python3 -m mypy packages/ingestion apps/api/app/documents`
   → `Success: no issues found in 25 source files`
6. Extra (not in spec, service-free): `pytest tests/unit/test_workspace_policy.py`
   → `32 passed` (29 pre-existing + 3 new Q3 unit tests)

Live evidence (this env: PG/Redis/Qdrant/Infinity all UP): standalone E2E probe
(upload unique phrase → index → keyword search HIT → DELETE 204 → chunks 0 /
FTS 0 / Qdrant 0 / keyword search `{"results":[]}` / detail 404
`document_not_found`) plus the committed integration tests above asserting the
same against dense (Qdrant scroll count), lexical (FTS row count + keyword
search), and ask (no citation) surfaces.

## Remaining limits (not claimed)

Q3 product acceptance, Q1/Q8/Q9, V2 graph, merge, deployment remain open. No
graph/answer-policy/compose/migration changes; other streams' edits untouched;
no commit performed (working-tree modifications to allowed files only).
