# Super RAG review — 2026-10-19

**Recommendation: request changes.** Reviewed `knowledge-os` at HEAD `473a553d897b825089bb1411400f00346675878e`, including the existing uncommitted graph, workflow and Search changes. Focus: recent review fixes, document lifecycle, queue delivery, retrieval and verification contracts, and configured quality gates. This is a focused code review, not full product acceptance or a comprehensive security audit.

No application code was changed. The review adds this report and offline reproduction artifacts.

## Findings

### 1. [P1] Workflow verification never evaluates the generated output

[`production_executor.py:200`](../packages/workflow_engine/production_executor.py#L200) receives `output` but never reads it. Instead, line 225 takes claims from the node's preconfigured `evidence` and sends those claims through verification. Successful checks of those claims mark the independent LLM output as verified.

**Reproduced:** the provider returned `The moon is made of cheese.`; the injected semantic verifier was called only with `The launch budget is 20 dollars.`; the returned output still carried `verified: true` and `verification_stage: Stage 11 & 12`. The recording verifier deliberately returns SUPPORTED to expose which text it receives; no remote model was called.

Extract claims from the actual generated output and establish coverage of that output before attaching a verification result. Configured evidence should supply references, not substitute for the text being checked.

### 2. [P1] Missing semantic verification is reported as a Stage 12 pass

[`production_executor.py:273`](../packages/workflow_engine/production_executor.py#L273) runs Stage 12 only when `evidence['verifier']` is non-null. When it is absent, execution falls through to the unconditional success result at line 297. Ordinary JSON workflow configuration cannot supply a live Python verifier object, and this executor does not construct one on this path.

**Reproduced:** providing a claim and matching canonical mapping without any verifier returned `verified: true`, labeled `Stage 11 & 12`, with zero semantic-verifier calls. Citation existence/provenance checks do not establish semantic support. `test_production_executor_verification_valid_claims_no_verifier` currently asserts this misleading success.

Resolve the semantic verifier through the runtime dependency boundary, or return an explicit incomplete/unavailable verification result. Claiming Stage 12 passed must require a successful Stage 12 report.

### 3. [P1] Graph deletion fence still allows a stale extraction to publish after deletion

[`sqlite_store.py:819`](../packages/graph/sqlite_store.py#L819) checks the document tombstone **before** `BEGIN IMMEDIATE` at line 839. The connection context manager does not itself start a transaction for these SELECTs. A different process can complete deletion between the check and acquisition of the write lock. The per-path asyncio lock only serializes store objects in the same process.

**Reproduced with real SQLite and an independent deletion process:** pause after reading the tombstone state, run `SQLiteGraphStore.tombstone_document` to completion in the child, then continue the upsert. The final state has `document_tombstoned: true` and a live newly inserted entity from that document. A connection proxy forces this possible interleaving; SQL results and commits remain real. Fresh entity IDs avoid the separate existing-row resurrection guard.

Acquire the write transaction before reading the tombstone and workspace-enable state. The worker's earlier tombstone check cannot close this later race.

### 4. [P1] Default deployment accepts queued agent runs into process-local stores

[`docker-compose.yml:137`](../docker-compose.yml#L137) still defaults `AGENT_STORE_DB` to empty. API and worker startup install shared SQLite stores only when it is configured; [`agent_stores.py:454`](../packages/stores/agent_stores.py#L454) otherwise supplies an `InMemoryAgentRunQueue`, with the run store likewise defaulting to memory. The queued-run endpoint persists/enqueues using these accessors and returns 202 without checking durable configuration.

**Reproduced:** enqueue through the default accessor in one process, then inspect it in an independent process: parent depth **1**, worker depth **0**. A separately deployed worker cannot consume those accepted jobs, and API restart loses the records. This earlier finding remains open even though the SQLite queue's claim race has been fixed.

Require a shared durable path when queued agents are enabled, or refuse admission until it is configured. Test API admission and worker consumption across independent processes.

### 5. [P2] Granular document deletion retains the deleted document's UUID chunk references

[`sqlite_store.py:1105`](../packages/graph/sqlite_store.py#L1105) removes chunk IDs only when they equal `document_id` or `c-{document_id}`. The production worker uses `str(ChunkRow.id)` for graph chunk IDs (`apps/worker/jobs/graph_extraction.py:150`): independent UUIDs, not those patterns. When an entity is supported by two documents, deleting one removes its document ID but retains its chunk IDs.

**Reproduced:** merge one entity from two documents using UUID provenance, delete the second document, and run `GraphRetriever.local_search`. The deleted document's chunk UUID is still in both the entity and returned graph evidence.

Persist a document-to-chunks association and remove the exact contributing references. Canonical answer hydration is a separate safeguard against citing deleted chunks; this reproduction establishes stale graph candidates, not an end-to-end citation bypass. Stale candidates can still consume retrieval limits and interfere with later lifecycle operations.

### 6. [P2] A late extraction can restore superseded-version graph rows

[`sqlite_store.py:712`](../packages/graph/sqlite_store.py#L712) deliberately reactivates rows retired by `tombstone_chunks` when no permanent document/entity tombstone exists. That permits a new version to reuse an entity ID, but also permits an old extraction to restore its old chunk IDs. `tombstone_chunks` records no durable chunk/version retirement fence, and the worker's publication check only asks whether the whole document was tombstoned.

**Reproduced:** publish version-one extraction, retire its chunks, verify its entity is absent, then publish the same stale extraction with `document_id` supplied. Three entities and one relationship become live again, and local graph search returns `old-version-chunk`. This models a worker that loaded old chunks before supersession and completed afterward.

Fence publication using document version/processing generation or durable retired chunk IDs. Allow replacement-version provenance while rejecting late publication of the retired version. As with finding 5, canonical hydration provides an additional boundary; the observed defect is graph lifecycle/retrieval state.

### 7. [P1] Required quality gates remain red

The current configured checks fail:

| Check | Observed result |
|---|---|
| `/root/ko-venv/bin/ruff check . --statistics` | **38 errors**, including undefined `Any`, an unused local, and new graph-store diagnostics |
| `/root/ko-venv/bin/mypy --cache-dir /tmp/super-rag-review-mypy-20261019` | **17 errors in six files**, 233 source files checked |
| `npm run lint` | One error: `react-hooks/set-state-in-effect` in Search at line 38 |
| `bash scripts/model_origin_guard.sh` | Rejects `.env.example`'s `DeepSeek` text and `gpt-4o-mini` default |

Restore the configured gates without weakening their intended contracts. The model-origin failures establish policy/configuration inconsistency, not execution of a prohibited model. The installed backend environment is Python 3.14; CI specifies Python 3.12, so dependency-sensitive type diagnostics can differ.

## Verification and limits

| Verification | Result |
|---|---|
| Frontend `npm test -- --reporter=dot` | **227 passed**, 25 files |
| Frontend `npm run typecheck` | Passed |
| Focused backend suites below | **106 passed, one failed**, one deprecation warning |
| Backend failure | `test_graph_lifecycle.py::test_purge_uses_store_purger_seam` requires PostgreSQL at localhost:5432; connection refused |
| Offline reproduction script | Six reproductions completed; network connections blocked; temporary SQLite only |
| `git diff --check` before review artifacts | Passed |

Backend command:

```sh
TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 timeout 150s /root/ko-venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/test_graph_store_sqlite.py \
  tests/unit/test_graph_store_concurrency.py \
  tests/unit/test_graph_lifecycle.py \
  tests/unit/test_graph_memory_tombstone.py \
  tests/unit/test_t_i01b_sqlite_agent.py \
  tests/unit/test_agent_queue_concurrency.py \
  tests/unit/test_workflow_production_executor.py \
  tests/unit/test_leg_failure_statuses.py \
  tests/unit/test_orchestrator.py
```

The successful SQLite queue concurrency tests and `BEGIN IMMEDIATE` before its ready-row SELECT support closure of the previous double-claim finding. Search now invalidates outstanding requests on workspace change and guards success, error, and loading completion; the earlier missing-invalidation finding is resolved in source, though a browser race was not independently replayed here.

The initial backend test and probe attempts stalled in the sandbox while awaiting Python thread results. Interrupted those attempts; a bounded diagnostic run timed out with the executor thread idle and event loop waiting. The listed completed backend tests and probes ran outside the sandbox with approval. No production service was started/restarted, no release published, and no paid model evaluation invoked. The full backend suite, live ingest-to-Ask acceptance path, and fresh Compose deployment were not executed.

The earlier first-run setup issue also remains visible in source: `.env.example` omits the required vector collection and workflow DB settings and supplies the rejected development JWT secret. That existing issue was not exercised with a fresh deployment in this review.

Reproduction source: [probes.py.txt](2026-10-19-super-rag-review/probes.py.txt). Results: [probes.json](2026-10-19-super-rag-review/probes.json). Run the source with `/root/ko-venv/bin/python probes.py.txt`; adjust `REPO` and its output location on another machine. The text extension keeps this standalone review artifact outside application lint/test discovery.

## Handoff

TASK: Review Super RAG project.
CHANGE: This report plus offline reproduction source and results.
WHY: Verify recent correctness fixes and record remaining delivery, verification, graph-lifecycle, and quality-gate defects.
REUSED: Existing production executors/stores/retriever, test suites, stdlib SQLite and subprocess; no new dependencies.
BUILT: Offline probes with temporary databases, recording providers and one controlled cross-process interleaving.
VERIFY: Commands, observations, and limits above.
RISKS: Findings require fixes before acceptance; full product behavior remains unverified. Existing implementation changes were preserved.
