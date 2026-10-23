# Super RAG review — 2026-10-15

Reviewed the current `knowledge-os` working tree, concentrating on production wiring, ingestion/retrieval lifecycle, and workspace changes in the UI. Initial HEAD was `8d7d1e0`; it advanced during review to `9d22794` through a documentation-only commit. Existing uncommitted work was preserved. This is a code review, not a live deployment or paid-model evaluation.

## Findings

### 1. [P1] Queued agents use separate in-memory queues and records in API and worker

`packages/stores/agent_stores.py:406` defaults the run store to `InMemoryRunStore`, and `:454` defaults the queue to `InMemoryAgentRunQueue`. No application startup installs another implementation. `apps/api/app/agents/lifecycle_routes.py:389` creates the record and enqueues it before returning 202, while the separately packaged worker reads its own process-local singletons. Starting the consumer in `apps/worker/worker/main.py` therefore does not make accepted API jobs visible to it. Runs remain queued and disappear on API restart; checkpoint and step defaults are volatile too.

Reproduced without services or LLM calls: called the real queued-run handler, then queried the defaults from a fresh Python process. API: accepted `queued`, queue depth 1. Worker process: queue depth 0, record `None`, claim `None`.

Use a shared durable queue and shared persistent run/checkpoint/step stores in both entrypoints. Refuse admission when that production wiring is unavailable. Validate acceptance and consumption across processes.

### 2. [P1] Shipped Compose configuration cannot initialize workflow stores

`apps/api/app/workflows/store.py:100` now requires `WORKFLOW_DRAFT_DB`; run, approval and checkpoint paths likewise require their respective variables. `docker-compose.yml:197` inherits a backend environment that provides none of those four settings, has no `env_file`, and supplies no workflow persistence mapping. Consequently the standard Compose API cannot list or create drafts: `get_draft_store()` raises outside the handler's error mapping (`apps/api/app/workflows/router.py:189`). Running also refuses because its checkpoint path is absent. Exporting these variables on the host alone does not forward them through this Compose file.

Reproduced the default route failure by calling the authorized real `list_drafts` handler with the four variables absent: `ValueError: WORKFLOW_DRAFT_DB is required`.

Wire all required store paths into Compose and place the databases on persistent storage. Map absent or unusable stores to an explicit capability-unavailable response, and exercise the deployed configuration in acceptance checks.

### 3. [P1] Lexical search can return tombstoned document text

`packages/ingestion/dedup.py:536` commits a document tombstone and removes canonical chunks before its separate FTS purge. A failed FTS purge is a supported outcome (DELETE returns 207), and there is also a normal interval between the tombstone commit and index cleanup. `packages/retrieval/adapters/pg_fts.py:178` searches only `lexical_docs`, with no document tombstone/current-version join. The orchestrator checks workspace membership, and `apps/api/app/search/search.py:258` returns the index text directly. Dense retrieval has an authoritative lifecycle gate, but lexical retrieval does not.

A retained FTS row from a deleted document can therefore appear in keyword and hybrid search after canonical reads already return 404. This concerns search text; the normal Ask hydrator supplies an additional canonical check.

Boundary probe using the real lexical adapter and orchestrator with a retained-row session double confirmed that the generated SQL contains no lifecycle predicate and the stale text is returned with status `ok`. No live database deletion was performed.

Join/filter against authoritative document/version lifecycle before lexical ranking, and verify deletion while FTS cleanup fails or remains in flight.

### 4. [P2] Search results and open passages survive workspace changes

`apps/web/src/app/search/page.tsx:23` stores results and the selected source independently of workspace identity. Changing `active` only changes the callback dependency; it does not clear or scope either state. An in-flight request for workspace A can also call `setResponse` after the user switches to B. The page and source panel consequently show A's content under B's active workspace, including until another search completes.

Key displayed state by workspace and request generation, clear the selected source on a switch, and discard or abort stale responses. Cover both an already displayed result and an in-flight search during a switch. This is a UI scoping defect, not evidence that backend workspace authorization is bypassed.

### 5. [P2] Backend test collection fails through a graph-evaluation module name collision

`evals/graph_paired_eval.py:51` imports the unqualified name `graph_paired_eval`, which is also the driver's own module name and the fixture helper's filename. Other collected modules prepend `evals` to `sys.path` (for example `tests/unit/test_q9_gates.py:20`). `tests/unit/test_graph_paired_eval.py:28` can then load the driver instead of the helper; the driver's import resolves to its own partially initialized module and raises `ImportError` for `DEFAULT_K`.

The full unit/contract invocation stopped during collection. A smaller deterministic reproduction also fails:

```sh
TMPDIR=/tmp /root/ko-venv/bin/python -m pytest --collect-only -q \
  tests/unit/test_q9_gates.py tests/unit/test_graph_paired_eval.py
```

Use distinct module names or qualified package imports instead of path-order-dependent top-level imports. Run the repository-wide collection check afterwards.

## Verification

- Frontend: `npm test -- --reporter=dot` — 23 files, 225 tests passed.
- Frontend: `npm run typecheck` — passed.
- Focused backend checks in `/root/ko-venv`: search endpoint, agent lifecycle, publication fencing, graph queue reliability, workflow configuration enforcement — 95 passed.
- Full backend `pytest -q -m 'unit or contract'` — blocked by finding 5 during collection; no full-suite pass is claimed.
- Fresh-process agent queue probe, missing-workflow-config route probe, and lexical retained-row boundary probe as described above.
- `.venv-test` lacks `python-multipart`; it was not used for the final backend evidence. Sandbox socket restrictions stalled TestClient even for a minimal application; final backend checks ran outside that sandbox after approval. Stalled review processes were terminated.

## Check of the completed Herdr review

After finishing the findings above, read the completed current-state review through the Herdr skill from `w5:pJ`. The separate GLM review was still running and was not interrupted. The completed review declares V1, V2, V3 and V4 ACCEPT, with remaining work characterized mainly as quality improvements.

That acceptance verdict is not supported by the production wiring:

- **V3 acceptance:** the API-to-worker queue and record isolation in finding 1 is reproduced across fresh processes. In-process lifecycle tests cannot establish that the packaged worker sees an accepted API run.
- **V4 acceptance:** a SQLite implementation and required-path checks are present, but the shipped Compose configuration cannot supply those paths. Finding 2 reproduces an authorized draft-list failure with the shipped defaults. Requiring durability does not establish that deployment wires it correctly.
- **V1 correctness:** finding 3 identifies a remaining deletion propagation gap in lexical search, separate from the canonical hydration checks used by Ask.
- **Checkpoint claim needs correction:** the completed review says checkpoint failures are counter-only with no log. Current `packages/workflow_engine/executor.py:628` both increments the counter and logs `checkpoint_append_failed` at `:632`. This particular complaint is outdated; it does not prove that checkpoint failures are sufficiently exposed in every API response.
- **Test environment claim needs correction:** missing multipart in the default Python environment and `.venv-test` is real, but the documented `/root/ko-venv` has it and passed the 95 focused tests. With that environment, whole-suite collection still fails on the independent module-name collision in finding 5. The collection failures cannot all be dismissed as an environment issue.

The completed review correctly recognizes several landed improvements: fail-closed handling of unparseable model policy, isolated optional-graph construction, graph deduplication/damping, optional rerank wiring, and shared approval/execution capability manifests. Its quality observations are also supported: the normal Answer factory selects `ExtractiveGrounder` (`packages/answer/factory.py:225`), and graph damping uses retrieval mode rather than query class (`packages/retrieval/orchestrator.py:662`). The quoted numerical graph-quality deltas were not independently re-evaluated here.

Recommendation: resolve the production queue, workflow configuration, and deletion-path blockers before upgrading acceptance; then measure the grounding and graph-routing proposals. No review or instructions were sent to another agent.

## Handoff

TASK: Review Super RAG current project.
CHANGE: This review document only; no product code changes.
WHY: Validate current production behavior and required checks against the operative plan and acceptance contracts.
REUSED: Existing project code, test suites, and documented test environment; no copied implementation.
BUILT: Review notes and temporary verification probes only.
VERIFY: Commands and outcomes above.
RISKS: No deployment, service restart, external messages, or paid model calls were performed. Whole-tree runtime verification remains incomplete because collection fails.
