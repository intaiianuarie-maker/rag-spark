# Super RAG follow-up review — 2026-10-17

Reviewed HEAD `c46927c85119a915fa575369371a37b7c35d31ad`. Application code and configuration are unchanged since the [earlier review](2026-10-17-super-rag-review.md); the active local changes are benchmark results and progress reports. This pass extends the review into workflow verification, graph extraction/persistence, and answer citation/semantic verification. It does not claim exhaustive coverage or production acceptance.

## New findings

### 1. [P1] Workflow nodes report Stage 11/12 verification without executing either stage

**Location:** `packages/workflow_engine/production_executor.py:179–184`.

After a single `provider.generate()` call, the LLM node handler sets `verified=True` and `verification_stage="Stage 11 & 12"` solely because its configuration contains `verify` or `requires_verification`. It never resolves canonical evidence, validates citations, invokes the semantic verifier, or evaluates the publication gate. The production workflow API selects this executor through `resolve_node_executor(default_factory=ProductionNodeExecutor)` at `apps/api/app/workflows/router.py:671`.

**Reproduction:** invoked the real executor with an admitted Gemma model, `requires_verification=True`, no source evidence, and a recording provider returning `The moon is made of cheese.`. The executor returned that sentence with **`verified: true`** and **`verification_stage: "Stage 11 & 12"`**. Exactly one provider call occurred. The provider was a local fake; no model endpoint was contacted.

The existing `test_production_executor_evidence_verification_flagging` asserts this unconditional badge, so its passing result protects the defect rather than testing verification. Downstream consumers receive a false claim of evidence checking.

**Required change:** run the shared canonical citation and semantic verification pipeline before emitting a verified result. If the node has no evidence or verification is unavailable, return an explicit unverified/unavailable outcome. Cover unsupported claims, missing/foreign/deleted citations, and verifier failure through the production executor, not only through standalone verifier helpers.

### 2. [P1] An in-flight graph extraction can resurrect a deleted document's graph entries

**Locations:** `apps/worker/jobs/graph_extraction.py:130–135, 254–264`; `packages/graph/sqlite_store.py:534–565`.

The worker loads a document version's chunks once, releases its database session, and then performs extraction and community summarization. Before publishing, it does not recheck document deletion, current version, or processing generation. The graph upsert explicitly resets `tombstoned=0`. If deletion/supersession completes after the worker's chunk read but before its upsert, the completed stale job makes those graph entries live again. The SQLite transaction makes the upsert atomic but does not fence it against the canonical document lifecycle.

**Reproduction:** ran the actual `process_graph_job` with a fake PostgreSQL read boundary, real rule extraction, and a real temporary SQLite graph store. Inside the extraction boundary, simulated the concurrent delete by calling the real `tombstone_document`. The entity was absent immediately after deletion. When the worker resumed and completed its normal upsert, **three live entities** existed again, and the job returned success with `partial=false`. Only one canonical database read occurred. Queue input and community summary were local fakes, and socket connections were blocked.

This proves resurrection of graph rows and stale graph retrieval state. The normal Ask path has a separate canonical hydration check; this probe does not claim that a deleted citation reaches a final Ask answer.

**Required change:** carry an authoritative lifecycle/generation token into graph publication and coordinate deletion/supersession with that publication fence. A check followed by an independent write alone leaves another race window. Test deletion and supersession while extraction is paused, then resume the actual worker and verify that no obsolete graph state becomes visible.

### 3. [P1] Graph ingestion replaces shared entities' earlier document provenance

**Location:** `packages/graph/sqlite_store.py:534–565`; related deletion behavior at `:742–759`.

Entity IDs are stable across documents in a workspace: the extractor derives them from normalized entity identity. Each queued job processes one document version, but `upsert_extraction` handles an existing entity/relationship with `payload=excluded.payload`. Consequently the next document's payload replaces earlier `chunk_ids` and `document_ids` instead of retaining all active supporting sources. Multi-document graph evidence becomes dependent on ingestion order.

**Reproduction:** used the real rule extractor and temporary SQLite store to ingest two separate documents containing `Alice Smith works at Acme Corp.`. After document 1, Alice's provenance was `[doc-1]/[chunk-1]`; after document 2, the same entity contained only `[doc-2]/[chunk-2]`. Deleting document 2 then removed Alice from the live graph altogether even though document 1 had not been deleted. This is independent of the concurrency issue in finding 2.

Simply unioning lists is insufficient: `tombstone_document` currently tombstones an entire node/edge if the deleted document appears anywhere in its provenance. Preserve source contributions separately, remove only the affected document/version's contribution, and retain entities/relationships while another active source supports them. Test repeated names/relationships across separately processed documents and deletion in both ingestion orders.

## Earlier findings remain open

No application/configuration changes have landed since the first review. Its default in-memory agent delivery, non-atomic SQLite queue claim, stale search response after workspace switching, failing quality checks, and incomplete setup configuration remain unresolved. This pass does not count the earlier test runs as new execution or claim that the previously reported gate failures were rerun.

## Verification and limits

Fresh targeted regression run: **239 passed in 6.82 seconds**:

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp /root/ko-venv/bin/python -m pytest -q \
  tests/unit/test_workflow_production_executor.py \
  tests/unit/test_answer_engine.py \
  tests/unit/test_citation_integrity_stage11.py \
  tests/unit/test_semantic_verification_stage12.py \
  tests/unit/test_graph_store_sqlite.py \
  tests/unit/test_graph_store_concurrency.py \
  tests/unit/test_graph_extraction_modes.py \
  tests/unit/test_graph_extractor.py
```

The successful tests do not cover the production compositions and interleavings above. Additional offline probes reproduced all three new findings using real production functions and temporary SQLite databases, with external boundaries replaced by recording fakes. Their complete output and source are in [2026-10-17-super-rag-followup/](2026-10-17-super-rag-followup/). Run the source attachment with `/root/ko-venv/bin/python probes.py.txt`; adjust its `REPO` constant on another checkout. The `.txt` extension keeps this standalone review artifact outside application lint/test discovery.

No product code was modified. Full backend execution, live ingestion/Ask acceptance tests, and paid model evaluation were not run. Existing benchmark activity and its local changes were preserved.

## Handoff

TASK: Repeat and extend the Super RAG review.
CHANGE: Follow-up report, offline reproduction source/output, and targeted test log.
WHY: Validate production verification claims and graph lifecycle/provenance behavior beyond the initial review.
REUSED: Existing production executor, rule extractor, SQLite graph store, worker function, and test suites; no new dependencies.
BUILT: Offline reproductions with temporary storage and fake external I/O.
VERIFY: Three reproduced defects and 239 passing targeted tests, as above.
RISKS: New findings require implementation fixes; previous acceptance blockers remain. Merge/deployment remains the coordinator's responsibility.
