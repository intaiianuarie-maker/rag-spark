# ROOT handoff — pre-existing test failures repaired (2026-10-27)

Date: 2026-10-27
Owner: root (verification lane) / Reviewer: coordinator + review1
Scope: HEAD `fe3c373`, working tree. No DONE marking, no push, no deploy.

## Why

Full unit suite on the dirty tree showed 21 failures. An independent pristine-HEAD
worktree comparison (`fe3c373`, same services/invocation) produced 29 failures
containing every one of the 21 — the working tree introduces zero regressions.
The failures were pre-existing HEAD drift; this handoff records the repair of 18
of them. Durable evidence:
`rag-fork-solution/tasks/evidence/root-20261027-dirty-tree-verification/verification-summary.md`.

## Fixes (four scoped commits)

1. **Stale offline stubs declare sparse vectors (T-I09 drift).** The store's
   sparse-declared check (`19403ed`, Oct 16) post-dates the offline stubs in
   `test_vector_store_boundary.py` / `test_invalid_vector_parking.py`; both now
   return `sparse_vectors={SPARSE_NAME: ...}` from `get_collection`, the shape
   verified against a live Qdrant 1.19 instance. Assertions unchanged.
   → 12 tests green again.
2. **Contract checker carries query identity (T-A05).**
   `packages/retrieval/contract_tests.py` searched without the identity its own
   fixture records stamp; the real adapter refuses at the dense boundary.
   Searches now pass `CONTRACT_IDENTITY`. → 1 test green again.
3. **Trigger-aware migration splitter + hermetic namespace tests.**
   `test_w7_parity_off_rollback.py::_statements` split on every `;`, breaking
   `CREATE TRIGGER ... BEGIN ... END;` bodies ("incomplete input"); now uses
   `sqlite3.complete_statement` accumulation → 2 tests green again.
   `test_ingest_pipeline` / `test_queue_semantics` / `test_purge_default_stores`
   were order-flaky: integration modules export process-wide env at *import*
   time (`test_dedup_versions.py:23` `INGEST_QUEUE_PREFIX=t071-dedup-:`,
   `vector_collection_env.py:41` `VECTOR_COLLECTION=knowledge_test_<tag>_<rand>`),
   so a full `-m unit` collection poisons later tests (observed:
   `queue_key() == 'ingest:t071-dedup-:queue'`). The three tests now pin their
   own namespace/collection — verified green under the exact polluted env.
   → 3 tests green again.

## VERIFY

```
TMPDIR=/var/tmp /root/ko-venv/bin/python -m pytest -q -m unit
# before: 21 failed, 2232 passed   after: 12 failed, 2241 passed
/root/ko-venv/bin/python -m ruff check <the six changed files>   # all clean
```

## Remaining 12 (all pre-existing / environmental, none tree-caused)

- 9 × `/tmp` tmpfs 100%-full artifacts (`t_a16` ×4, `v4_authoring` ×4,
  `bulk_chunk_lookup` ×1): trio-scope tests hardcode `/tmp/opencode` DB paths
  (os.environ.setdefault + monkeypatch.setenv). Proven environmental: with
  `WORKFLOW_*_DB` redirected to `/var/tmp`, 17/17 pass. `/tmp` cannot be safely
  cleared — 903 MB is another lane's active job (today 13:43), kaggle dirs
  off-limits per team decision.
- 1 × `v4_authoring_api::test_run_workflow_runtime_failure_returns_coded_error`:
  stale expectation — model-origin guard (PD-005) now refuses with "Model 'x' is
  not admitted for workflow role" vs the test's old "LLM node" prefix. Trio-scope
  file; owner: T-IMP-001/T-RV19 review.
- 2 × owner policy decisions: `answer_lost_region_disclosure` (engine's
  T-OR-12 `NEEDS_REVIEW` semantics vs the Oct-13 test's `UNAVAILABLE`), and
  `harness_hygiene::test_no_test_requires_an_api_key` (committed
  `test_openrouter_provider.py:419` env read vs the hygiene rule).

## NOT committed (pending decisions)

- `apps/api|worker/{Dockerfile,requirements.txt}`: `certifi==2026.10.19` does NOT
  remove the emSign roots (measured: 2026.10.18 = 0 roots, .19 = 3, .23 = 1).
  Decision needed: pin .18, or move to .23 and fix the comment. The pip-26.2.2
  Dockerfile pins are clean and can commit independently.
- `tests/fixtures/scripts/live_multilingual_eval{,_cli}.py`: fixture rework —
  diff-reviewed, not executed; needs the live-stack owner.
- `apps/web/src/app/search/page-teeth-r3.tsx`: superseded iteration (the evolved
  `page.tsx` is committed); recommend deletion, kept untracked meanwhile.

## RISKS

Test-only changes except `packages/retrieval/contract_tests.py` (shared checker —
only consumers: vector-store contract test + harness hygiene scan, both green).
No assertion weakened; three tests became order-deterministic.
