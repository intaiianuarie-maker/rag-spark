# Super RAG v1–v4 review

14 October 2026 · Contabo `vmi3584180` · Independent source and deployment review

**What this repo does:** Knowledge OS is a workspace-based document search and question-answering platform. V1 supplies ingestion, retrieval and cited answers; V2 adds a knowledge graph; V3 adds agents and research; V4 adds visual workflows. This review assumes a small shared deployment with concurrent users, background jobs and process restarts—not a single-process demonstration.

**Verdict: do not treat all four versions as completed products.** There is substantial implementation and test coverage, but the running system has a known default authentication secret, a heavily contaminated vector index, and V3 endpoints that report simulated work as completed. V2 and V4 still lack essential deployment or product wiring.

The companion **fix plan** orders containment, safe index recovery, real acceptance tests, and version-by-version completion. No product source, configuration, service, credential or production data was changed during this review. Verification used read-only inspection, existing unit/contract tests, and isolated in-memory probes.

## Version assessment

| Version | What is present | What prevents product acceptance | Assessment |
|---|---|---|---|
| **V1 — core RAG** | Auth/workspaces, parsing, upload worker, dense/lexical retrieval, reranking, canonical citation hydration, deletion fences, Ask/Search UI | Default signing key; contaminated index; unsafe initial index switch; cross-language answer verification absent; live quality targets unproven; continuous connectors not wired | Substantial implementation, **not release-ready** |
| **V2 — graph** | Extraction, SQLite graph store, retrieval/fusion, provenance, queue and extensive tests | Graph disabled in deployment; no graph consumer started; no shared graph volume; enqueue failure hidden; paired artifact has no successfully cited answers | Library implementation, **integration incomplete** |
| **V3 — agents/research** | Budget/tool policies, evidence/step/checkpoint components, API routes and UI source | Default agent is a no-op; research fabricates citations; API state is process-local; actual queued durable execution is not wired; deployed frontend lacks V3 routes | **Exposed scaffold, not functional product execution** |
| **V4 — workflows** | Restricted import, validation, compilation, bounded executor, revision/checkpoint contracts, hostile-input tests | No workflow HTTP handlers or UI; production V3 runner not connected; draft storage remains in memory; persistence SQL is a draft | **Useful foundation, intentionally inert** |

The versions are stages within `/root/code/Super Rag/knowledge-os`, not four separate deployed applications. The controlling plans are in `/root/code/Super Rag/rag-fork-solution`.

## Scope and evidence

- Source snapshot began at `acf7655e236dc2b0908827480d7adb56b7529e5f`. The checkout was detached and contained uncommitted work.
- Another actor committed `daf9f5114f133ec19c4a05f17c48260959dd17c6` during review. Its changes were the 2WikiMultihopQA acquisition module, its tests and a handoff; the product files supporting these findings did not change in that commit.
- Inspected source includes authentication, API factories, ingestion/publication, retrieval/index promotion, answer grounding, graph queue/worker/storage, agent/research entry paths, workflow persistence/runtime contracts, frontend entry points, CI and deployment configuration. This was a risk-focused review, not a line-by-line audit of every file.
- The repository has no `.codegraph/` index. File and symbol searches were used without creating one.
- The deployed API's agent/research files and the worker entry point matched the reviewed source by SHA-256. The API exposes the V3 routes in its live OpenAPI document.
- The tracked-source archive SHA-256 is `3bc2878539e722e96ade633d3a72f27d69589794845bc8bf20e9a7910e6acc89`.
- Source references below are relative to `/root/code/Super Rag/knowledge-os` and refer to that snapshot.

### Checks run in this review

| Check | Result | Meaning |
|---|---|---|
| Backend `pytest tests/unit tests/contracts -q -p no:cacheprovider` | **2,264 passed, 1 failed, 1 skipped**, 327.70 seconds | Failure: `GRAPH_ENQUEUE_FAILED` missing from error taxonomy. Skip: optional 330 MB dataset audit. The suite included then-untracked 2Wiki tests. |
| Ruff, entire working tree, no cache | **Failed: 1 E501** | `tests/unit/test_ingest_pipeline.py:159` |
| Mypy, nonincremental | **Passed: 195 source files** | Type checks, not proof of product wiring |
| CI vendor-import guard | **Failed: 3 matches** | `llama_index` outside adapters in ingestion; two Redis imports in graph queue |
| Frontend Vitest | **216 tests passed in 21 files** | Current checkout, not the older deployed web image |
| TypeScript `tsc --noEmit --incremental false` | **Passed** | Same command chain exited zero |
| Isolated behavior probes | **Defects reproduced** | Research fabrication, no-op agent, lost route state, swallowed graph enqueue, unsafe index switch, default-key validation bypass, missing session-subject binding, cross-language rejection, mutable workflow SQL |
| Live index census/reconciliation | **Read-only, all 2,041 points examined** | Counts below; no vectors or document text exported in deliverables |

### Running deployment

The API and worker mount the editable source tree; the API uses `uvicorn --reload`. The web container is a built image with no source mount, started on 12 October. Its built route manifest contains **no agent, research or workflow pages**. `/version` returns `git_sha: "unknown"`.

Two `/ready` samples returned `ready:false`: PostgreSQL timed out at the probe budget and Infinity could not be reached. A direct local `/models` request to Infinity also reset. **The final readiness check recovered to `ready:true`, with all seven dependencies reachable.** PostgreSQL was also accessible to the separate reconciliation read, so these observations do **not** establish a persistent database outage. The embedding container had recently restarted/recreated while other project activity was ongoing; the transient failure's cause was not established. Docker called the API healthy during the degraded interval because its health check only tests `/health`.

The API/web are published on all interfaces at ports 8000/3001; backing stores are correctly bound to loopback. `APP_ENV=local` and `COOKIE_SECURE=false` are active. External firewall/TLS reachability was not tested. Host snapshot: 7.7 GiB RAM, 2.9 GiB available, 4.2 GiB swap used; disk 79% used with 42 GB available. These are observations, not a capacity benchmark.

## Must fix

### 1. P0 — Known signing key and incomplete session binding permit identity forgery

**What this is:** JWT access tokens identify the user, while a Redis session is intended to make them revocable. The running API is using a known development signing key; only a boolean comparison was recorded, never the credential value.

**Problem:** The production validator rejects a different development literal than the one Compose supplies. An isolated probe accepted the Compose default even with `APP_ENV=production`; another resolved a victim subject using a different user's active-session ID because session validation checks existence, not ownership.

**Fix:** Require an operator-provided random signing key, reject all shipped defaults at startup, rotate the deployed key and invalidate affected sessions. Resolve the Redis session record and require its `user_id` to equal the token subject on every access-token request.

**If we skip it:** A caller who can obtain a valid session and target user ID can forge that user's identity using the known key. This was reproduced with fake session/database boundaries; no real account was impersonated and no evidence of prior exploitation was assessed.

**Evidence:** `docker-compose.yml:105`; `apps/api/app/auth/config.py:42–49`; `apps/api/app/authz/principal.py:67–94`; `apps/api/app/auth/adapters/redis_sessions.py:23–40`. Probe results: `production_accepts_compose_default_jwt=true`, `victim_subject_with_other_active_session_accepted=true`.

### 2. P1 — The deployed research endpoint invents source-backed results

**What this is:** The research API should investigate a topic and return findings supported by stored documents.

**Problem:** `create_research_pipeline()` returns `None`; the default handler copies the topic into a “grounded sentence,” assigns `chk-1`/`ev-1`, and returns `completed`. Keywords also manufacture unknowns and contradictions; neither documents nor real evidence are consulted.

**Fix:** Remove these defaults from the production path and return an explicit unavailable response until a real pipeline is configured. Then wire planning, scoped retrieval, canonical evidence verification and synthesis; persist and revalidate every returned citation.

**If we skip it:** A user-supplied assertion becomes a falsely verified research result. The isolated probe submitted an invented office budget and received it back with `chk-1`, no limitations, and `abstained:false`.

**Evidence:** `apps/api/app/agents/research.py:60–65,116–176`; `apps/api/app/main.py:117–118`. The handler's SHA-256 matched the live container and its route is mounted.

### 3. P1 — Agent execution is a no-op and API lifecycle state is not durable

**What this is:** Agent runs should execute tools under budgets and retain a recoverable run history.

**Problem:** The default factory uses `_NoopLLM`, which always returns `Final Answer: done`; a request to calculate `17 × 19` completed with output `done`. The API awaits execution before recording/returning the run, keeps status in `_RUNS`, and research uses `_RESEARCH`; runtime checkpoint defaults are also in-memory.

**Fix:** Refuse execution when the real provider/tool set is missing. Persist the run before returning `202`, dispatch it to a supervised worker, and connect durable state/checkpoint/evidence stores to status, cancellation, approval and resume handlers.

**If we skip it:** Users receive false completion, cannot reliably discover/cancel an executing run through this API, and lose history on restart or across multiple API processes. Clearing only the route's in-memory store made an existing run return 404 in the probe.

**Evidence:** `apps/api/app/agents/router.py:41–72,151–181,185–248`; `packages/agents/runtime.py:156–183,193–216`; `apps/worker/worker/main.py:50`; `packages/agents/worker.py:29–62`. Component-level durable-store classes exist; the problem is the active wiring.

### 4. P1 — The live index is contaminated, and normal ingestion omits embedding identity

**What this is:** Dense retrieval assumes document and query vectors represent the same model and revision.

**Problem:** All **2,041** live points lack recorded model/revision/dimension metadata. **1,651** vectors have identical components, **276** have zeros after component 384, and only **114** have another shape; the last group is not proof of authenticity. Normal ingestion does not attach embedding identity, although the rebuild script does.

**Fix:** Add provider/model/revision/dimension identity to normal writes and enforce collection compatibility for both writers and query providers. Build a fresh index from explicitly selected, current, nondeleted source versions with the real embedder; quarantine test debris and classify unmatched records before any cleanup.

**If we skip it:** Retrieval rankings cannot be trusted, and a rebuilt collection immediately starts accumulating unverifiable records again. The reconciliation also found **114 orphan points**: 67 superseded, 7 tombstoned, 40 unexplained, plus **1,855 PostgreSQL chunks without a point in this collection**; those chunks include other scopes/test data and are not all established production losses.

**Evidence:** live read-only census; `apps/worker/jobs/ingest_pipeline.py:984–998`; `scripts/reindex_all_chunks.py:185–198,263–296`; historical mixed-index disclosure in `handoffs/MUSER-LOOP-STATE.md:206–218,243–257,546`.

### 5. P1 — Initial index promotion deletes the rollback target before switching

**What this is:** `promote_replacement()` switches a completed replacement index into service.

**Problem:** When the live name is a physical collection, it deletes that collection and only then creates the alias. Injecting a failure into alias creation left neither the old collection nor the live alias, although the replacement survived; the exception handler cannot restore service.

**Fix:** Introduce a distinct stable serving alias, initially pointing to the retained old collection, and move all readers/writers to it in a controlled release. Subsequent promotions can atomically repoint that alias while preserving the prior collection and a tested rollback; do not use deletion to convert the current serving name.

**If we skip it:** The planned repair of finding 4 can itself create an outage and destroy the immediate rollback copy. Fix and test this path **before** rebuilding/promoting the live corpus.

**Evidence:** `packages/retrieval/adapters/qdrant_store.py:569–615`; `scripts/reindex_all_chunks.py:530–562`. Stub-client failure injection: `old_collection_survives=false`, `live_alias_exists=false`, `replacement_survives=true`. No real collection was changed.

### 6. P1 — V2 graph ingestion has no deployed consumer or shared store

**What this is:** Ingestion enqueues graph work, and Search/Ask read a SQLite graph.

**Problem:** `run_graph_worker()` is defined but never called anywhere in the reviewed repository; the deployed worker starts ingestion only. Compose does not pass `GRAPH_MODE`/`GRAPH_DB_PATH` or mount a shared graph store, so graph defaults off and independently enabling it would leave API/worker stores separate.

**Fix:** Add an explicit supervised graph consumer and a persistent store accessible consistently to extraction and retrieval. Pass the graph settings, wire per-workspace enablement, and expose queue age, failures and consumer heartbeat before enabling the feature.

**If we skip it:** Enabling graph mode can queue work indefinitely or read an empty/private graph file. Passing graph unit tests does not make the deployed product ingest or retrieve a graph.

**Evidence:** `apps/worker/jobs/graph_extraction.py:383–430`; `apps/worker/worker/main.py:15,50`; `packages/graph/factory.py:36–67`; `docker-compose.yml:73–107,194–212`. Live environment had no graph override; process inspection found only the ingest worker.

### 7. P1 — Graph enqueue failures are logged as successful publication

**What this is:** The ingestion pipeline expects graph enqueue errors to raise so it can retry.

**Problem:** `enqueue_graph_extraction()` catches every exception and returns `None`; its caller ignores the result, logs `graph_job_enqueued`, and can finish READY. Separately, the caller's intended `GRAPH_ENQUEUE_FAILED` exception code is absent from the shared taxonomy, which is the failing contract test.

**Fix:** Propagate a typed enqueue failure, require a valid durable job receipt, and register the canonical error/retry behavior. Keep “graph disabled” separate from “graph enabled but queue unavailable.”

**If we skip it:** A broker failure permanently loses graph work while the document appears successful; observability and retry policy report the wrong outcome.

**Evidence:** `apps/worker/jobs/graph_extraction.py:58–98`; `apps/worker/jobs/ingest_pipeline.py:1095–1132`; `tests/contracts/test_error_taxonomy_contract.py:188`. Injected queue failure returned `None` without raising.

### 8. P1 — Acceptance evidence does not establish the claimed RAG quality

**What this is:** Q9 and G5 are supposed to establish release quality and graph benefit.

**Problem:** Q9 supplies gold context and gold answer text, then evaluates verification; it does not run real retrieval or generation, and the citation check can succeed on an empty surviving set. The saved 20-row G5 paired artifact contains **zero cited answers** in either arm: baseline has 13 refusals/7 nulls, graph has 12 refusals/8 nulls; lexical scores still assign positive values to refusal text.

**Fix:** Keep the existing harness as a verification unit benchmark. Add a separate live product gate with a frozen corpus, actual questions, real retrieval and generation, answer correctness, citation coverage/validity, Recall@20, abstention, per-language/slice results and paired uncertainty for graph comparisons.

**If we skip it:** A green score can coexist with a broken index, a nonanswering model or zero useful graph answers. The acceptance record itself acknowledges that factual precision and Recall@20 were unmeasured and the multilingual acceptance probe was not exercised.

**Evidence:** `evals/q9_run_benchmark.py:87–117,139–181`; `evals/g5_real_paired_eval.py:143–170`; `evals/g5_paired_results.json`; `apps/api/app/eval/metrics.py:108–145`; `handoffs/T-V1-036-ACCEPTANCE-RECORD.md:33–47,84–119`. Saved-artifact mean lexical scores were 0.160 baseline and 0.145 graph; these are not answer-success rates.

### 9. P1 — V1 cannot verify the promised cross-language answers

**What this is:** The V1 acceptance plan requires Romanian answers grounded in English/Romanian/Portuguese documents.

**Problem:** The default grounder requires a verbatim claim within the source text, so a correct translation is rejected. The probe scored “Capitala Frantei este Paris.” against “The capital of France is Paris.” at **0.0**, below its **1.0** threshold; the implementation explicitly documents this unmet limitation.

**Fix:** Keep the strict extractive verifier for source quotations and add a separately tested cross-language verification path with original-source spans and conservative abstention. Until validated, label this capability unavailable and narrow the release claim; do not lower the substring threshold to force a pass.

**If we skip it:** The promised multilingual experience either refuses correct answers or returns source-language quotations instead of the requested language. A multilingual embedding model alone cannot fix this answer-verification gap.

**Evidence:** `packages/answer/verification.py:34–89`; `packages/answer/factory.py:128–144`; `../rag-fork-solution/TESTING.md`, acceptance #5; isolated grounding probe.

## Should fix / complete before claiming the affected feature

### 10. P2 — Continuous connector sync is library-only

**What this is:** V1 M6 promises ongoing Web/S3/Git synchronization in addition to upload.

**Problem:** `QueuedSyncConsumer` has implementations and tests, but its only instantiations in the reviewed tree are tests. No connector routes appear in live OpenAPI and no sync consumer is started by the shipped worker.

**Fix:** Add the smallest operational surface: workspace-authorized connector configuration, secret references, scheduling/enqueueing, a supervised consumer and visible cursor/error status. Alternatively, explicitly ship V1 as upload-only and leave M6 incomplete.

**If we skip it:** Users cannot configure or operate the advertised continuous synchronization through this deployed product.

**Evidence:** `packages/connectors/sync_consumer.py:414–492`; `apps/worker/worker/main.py:50`; `apps/api/app/main.py:102–118`; whole-tree constructor search and live route inventory.

### 11. P2 — V4 is not yet a usable workflow product

**What this is:** V4 should let users author, approve, run and inspect revisioned workflows.

**Problem:** Workflow routes are schemas only; no handlers are mounted, no workflow UI exists, and `DraftStore` has only an in-memory implementation. The V3 adapter requires an injected production runner, while its disabled resolver defaults to `FakeNodeExecutor`; deployed-as-inert is accurately a safety state, not product completion.

**Fix:** Keep execution disabled while implementing durable drafts, revision-scoped approval, real V3 dispatch, authorized HTTP handlers and a minimal author/run/status UI. Put simulation executors behind test-only construction paths.

**If we skip it:** The component suite can remain green while users have no runnable workflow feature. Enabling a flag alone will not supply the missing integration.

**Evidence:** `packages/workflow_engine/routes/__init__.py:1–11`; `routes/schemas.py:1–13`; `draft_store.py:29–33,217`; `adapters/v3_adapter.py:160–197`; `apps/api/app/main.py:102–118`; live OpenAPI and web route manifest.

### 12. P2 — Release evidence cannot be tied to one tested deployment

**What this is:** Backend, worker and frontend should ship a reviewed, identifiable version with all required checks passing.

**Problem:** Live backend source is mutable and auto-reloading while the web image is older; `/version` reports an unknown commit. Current CI would fail its taxonomy test, Ruff and vendor-import guard, and a healthy Docker label can coexist with `ready:false` because it checks process liveness only.

**Fix:** Use a separate production Compose configuration with immutable images built from one commit, meaningful revision labels and no source mounts/reload. Make release promotion require the complete checks and a capability/readiness smoke; keep liveness and readiness visibly separate, and serve authenticated traffic through the intended TLS access path.

**If we skip it:** A source edit can change the API without an intentional release, UI/API behavior drifts, and old acceptance numbers are mistaken for evidence about the running system. The observed transient dependency degradation also needs explanation before claiming stable release readiness.

**Evidence:** `docker-compose.yml:194–198,210–212,215–234`; `apps/api/Dockerfile:26–27`; `apps/api/app/version.py:10–18`; `.github/workflows/ci.yml:24–56`; fresh check results and live image/mount metadata.

### 13. P2 — V4 draft SQL does not enforce its claimed immutable history

**What this is:** A workflow approval must refer to a revision that cannot silently change.

**Problem:** The draft migration says its composite primary key prevents UPDATE/DELETE and backwards revision insertion. An in-memory SQLite probe applied the exact SQL, successfully updated and deleted revision 1, and inserted revision 2 after revision 3; a primary key only prevents duplicate keys.

**Fix:** Before applying the migration, define and enforce append-only writes using an appropriate restricted database role or immutable-history triggers, plus transactional revision allocation. Correct the claims about workspace isolation too: including workspace in a key does not itself enforce authorization on a query.

**If we skip it:** The eventual durable backend can rewrite approved history while its schema is mistakenly treated as protection. This is a pre-integration defect; no deployed workflow data was shown to be vulnerable.

**Evidence:** `packages/workflow_engine/migrations/0001_workflow_draft_revisions.up.sql:9–17,34–53`; `migrations/README.md:60–91`; scratch-only SQL probe.

## What is working and should be retained

The project has useful boundaries and defensive components: workspace-scoped retrieval, canonical source hydration, deletion/version fences, explicit dependency failures, input limits, tool budgets and restricted workflow imports. Backend type checking and all 216 frontend tests passed. Backing database, queue, vector and object-storage ports are bound to loopback. The safe course is to complete the real entry paths and repair deployment/data integrity around these components, not replace the project wholesale.

**Verdict:** contain findings 1–3 immediately; fix promotion safety before index recovery; prove V1 and V2 on real data before exposing V3/V4 execution as completed products.

**Not checked:** fresh clean-container installation/build, dependency CVE/license audit, external firewall/TLS access, backup restoration, paid/cloud model generation, live writes/deletes, destructive crash tests, full integration suite, real throughput/load, every parser format or every source file. Existing historical integration reports were inspected but not relabeled as tests run today. Other operators were active during the review; runtime observations are timestamped snapshots.
