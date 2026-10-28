# Super RAG current-state review — follow-up, 2026-10-15

**Status: unfinished draft.** Reviewed working tree at `9d22794`. Existing uncommitted changes were preserved. This is a general code review, with focused investigation of workspace policy enforcement; it is not a comprehensive security audit. No product code was changed.

The earlier report is [2026-10-15-super-rag-review.md](2026-10-15-super-rag-review.md). This pass independently checked the following findings against current source. Evidence from the earlier review is not counted as newly executed verification.

## Current findings

### 1. [P1] API and worker use different in-memory agent queues

`packages/stores/agent_stores.py:406–410` defaults to `InMemoryRunStore`, and `:454–460` defaults to `InMemoryAgentRunQueue`. The API persists/enqueues through these accessors in `apps/api/app/agents/lifecycle_routes.py:389–393`. The separate worker uses the same accessors in its own process (`apps/worker/jobs/agent_worker.py:53`). No startup wiring installs shared implementations. Accepted runs therefore cannot reach the packaged worker, and their records disappear on API restart.

Fix: install shared durable queue and run/checkpoint/step stores in both entrypoints, and verify admission and consumption across processes. Current evidence: source trace; earlier report additionally contains a fresh-process reproduction.

### 2. [P1] Compose omits required workflow database configuration

`apps/api/app/workflows/store.py:100–113` requires `WORKFLOW_DRAFT_DB`; run, approval, and checkpoint storage also require configured paths. `docker-compose.yml` forwards none of these settings and supplies no workflow persistence volume. An authorized draft listing reaches `get_draft_store()` outside error mapping (`apps/api/app/workflows/router.py:189`) and raises with the shipped configuration.

Fix: forward all four database paths, mount persistent storage, and return an explicit unavailable response when configuration is incomplete. Current evidence: source/configuration trace; earlier report contains a direct-handler reproduction.

### 3. [P1] Lexical search can return deleted document text

`packages/ingestion/dedup.py:534–550` deletes canonical chunks and commits the document tombstone before separately purging FTS. The lexical query (`packages/retrieval/adapters/pg_fts.py:172–181`) reads only `lexical_docs`, without checking the authoritative document/version lifecycle. Search returns that index content directly (`apps/api/app/search/search.py:258–274`). During cleanup, or after a supported FTS purge failure, keyword/hybrid search can show deleted text. Ask has an additional canonical hydration check.

Fix: enforce authoritative lifecycle filtering in lexical retrieval before ranking. Cover both delayed cleanup and failed cleanup. Current evidence: complete source trace; no live database deletion performed.

### 4. [P2] Search results remain visible after switching workspaces

`apps/web/src/app/search/page.tsx:24–31` stores response and selected passage independently of workspace identity. The request callback (`:35–58`) has no request-generation guard, and there is no workspace-change reset. Switching A → B retains A's results/source panel; an outstanding A request can also overwrite B's displayed results.

Fix: scope displayed state to workspace identity, clear selection on a switch, and abort or discard stale responses. Current evidence: component/provider source trace. This does not establish a backend authorization bypass.

### 5. [P2] Backend test collection has a module-name collision

`evals/graph_paired_eval.py:51` imports the unqualified name `graph_paired_eval`, shared with the fixture helper. When `tests/unit/test_q9_gates.py` precedes `tests/unit/test_graph_paired_eval.py`, path mutation causes the driver to import itself. This pass reproduced `ImportError: cannot import name 'DEFAULT_K' from partially initialized module 'graph_paired_eval'`.

Reproduction:

```sh
TMPDIR=/tmp /root/ko-venv/bin/python -m pytest --collect-only -q \
  tests/unit/test_q9_gates.py tests/unit/test_graph_paired_eval.py
```

Fix: give the modules distinct names or use qualified package imports, then verify whole-suite collection.

## Additional candidates awaiting bounded runtime validation

### A. Ingestion may bypass local-only workspace policy

`apps/worker/jobs/ingest_pipeline.py:961–969` unconditionally constructs `OpenRouterProvider(call_site="ingest_pipeline")` and passes chunk text to `embed_documents`. Neither the upload handler nor the ingestion pipeline loads the workspace's sensitive/local-only policy. The provider defaults to the external OpenRouter endpoint when a key is configured, and its `require_local` parameter defaults to false. In contrast, the retrieval factory explicitly refuses cloud embedding for sensitive workspaces.

Source-grounded consequence: an authorized upload to a sensitive workspace appears able to send document text to the cloud despite local-only policy. Pending: a dummy sensitive workspace, real pipeline control flow, and a recording HTTP transport inside a network-disabled sandbox. No external request or real document is needed. This is not yet presented as a runtime-confirmed security finding.

### B. Lost-page disclosure appears absent from retrieval indexes

The pipeline merges `chunk_disclosure` into `ChunkRow.chunk_metadata` (`apps/worker/jobs/ingest_pipeline.py:856–859`) but constructs FTS and vector metadata from `c.metadata` without that merge (`:904`, `:1014`). `PostgresChunkLookup` does not select chunk metadata, and `CanonicalHydrator` retains the retrieved metadata. Thus a partially scanned document may be marked `needs_review` while an answer over its readable text receives no lost-page warning.

Pending: capture real pipeline output for the existing mixed-scan PDF fixture and compare canonical chunk metadata with both index records and answer disclosure. The existing downstream disclosure tests seed the warning metadata themselves, so their success does not settle this ingestion boundary.

## Verification from this pass

- Frontend `npm test -- --reporter=dot`: **225 passed, 23 test files**.
- Frontend `npm run typecheck`: **passed**.
- Targeted backend collection reproduction above: **failed as described**.
- A focused backend run did not complete; it showed a failure before stalling and was interrupted. No backend-suite pass is claimed.
- An isolated namespace smoke check passed after permission was granted; candidate probes have not yet run.
- No deployment, restart, live data mutation, external messages, or paid-model calls were performed.

## Remaining work

1. Validate candidates A and B with dummy inputs and recorded transport/store boundaries.
2. Complete a bounded, service-free backend test selection.
3. Update this draft with confirmed outcomes and final priorities.

User constraint: stop when 5% of the five-hour allowance remains. The assistant cannot currently observe the account quota; this limitation was disclosed to the user.
