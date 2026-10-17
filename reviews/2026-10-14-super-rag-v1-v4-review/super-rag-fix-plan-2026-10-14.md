# Super RAG repair and completion plan

14 October 2026 · Based on the independent v1–v4 review

**Recommended order:** contain authentication and false-success endpoints → make index switching safe → restore a clean V1 retrieval path → complete V2 wiring and measure it → connect real V3 execution → finish V4 product surfaces.

This is a proposed execution plan, not an applied change. The existing coordinator remains the release owner. No tickets were marked complete, no other agents were messaged, and no deployment or data rewrite was performed.

## Phase 0 — Contain current user-facing risks

| Task | Concrete change | Owner | Acceptance check |
|---|---|---|---|
| **R01 — authentication** (finding 1) | Replace the known signing secret through deployment secret management; revoke existing sessions; reject missing/default secrets; bind Redis session owner to JWT subject | Backend + release owner | Both shipped default strings rejected; forged subject with another account's valid session rejected; normal login/refresh/logout still work; old tokens fail after rotation |
| **R02 — honest unavailable states** (findings 2–3) | Remove no-op agent and synthetic research defaults from production factories; keep unfinished execution disabled or return a structured unavailable response | Backend | Unconfigured agent/research requests never return completed, never synthesize `chk-1`, and make no provider/tool call |
| **R03 — controlled deployment baseline** (finding 12) | Record source commit, image digests, config names and enabled features; diagnose Infinity failures and PostgreSQL readiness timeouts; separate liveness from capability readiness | Release owner / operations | Stable dependency readiness and a real local embed/rerank smoke; version endpoint identifies the build; outage injection on staging marks the relevant capability unavailable |
| **R04 — access path** (findings 1,12) | Use the intended TLS entry point with secure cookies; restrict direct backend access to the actual operational requirement after checking existing routes/firewall needs | Operations | Login/refresh work through TLS, cookies are Secure, and unintended plaintext/direct entry paths are closed |

Key rotation intentionally logs users out. Prepare and verify the configuration first, then coordinate that cutover. Retain the current data and images while preparing recovery.

## Phase 1 — Restore V1 data and release integrity

### R05 — Fix index switching before touching the live index

Scope: `packages/retrieval/adapters/qdrant_store.py`, `scripts/reindex_all_chunks.py`, deployment configuration and focused failure tests.

1. Inventory physical collections and aliases and take a restorable backup of originals, PostgreSQL and Qdrant.
2. Introduce a **new stable alias** that initially points to the existing physical collection. Do not delete `knowledge` to reuse its name as an alias.
3. Explicitly pass `VECTOR_COLLECTION` to both API and worker, and move readers/writers to the stable alias in a reviewed release.
4. Promote only by one atomic alias update. Retain the previous physical collection until the rollback window closes.
5. Inject failures before verification, during alias update, and immediately after it. Every case must leave a readable current or previous index and a documented recovery command.

Exit: interrupted promotion cannot remove the serving index or its rollback copy. A staging rollback is demonstrated, not just described.

### R06 — Add identity enforcement to every embedding write and query

Scope: ingestion record creation, embedding adapter/collection boundary, rebuild records and reader factory.

- Record provider, exact model revision, dimension and chunk/document version. Apply the same schema to normal ingestion and rebuilds.
- Check collection identity against the configured query/writer provider before using it. Reject unknown or incompatible identity instead of padding/truncating.
- Make identity validation independent of arbitrary chunk metadata so callers cannot overwrite trusted embedding fields.
- Add a test where normal ingestion writes to a newly rebuilt collection and its full identity audit still passes.

Exit: both ordinary and rebuild writes carry verified identity; incompatible providers fail explicitly before any write or search.

### R07 — Rebuild a clean corpus in a reserved capacity window

Depends on R03, R05 and R06. This is the first planned operation that changes production data selection or serving configuration.

1. Freeze a source manifest: authorized workspaces, **current** document versions, original hashes and expected chunk IDs. Exclude deleted/superseded versions and explicitly identified test fixtures. The existing rebuild query only filters `documents.deleted_at`; tighten it to the current-version contract before use.
2. Classify the 114 orphan points and the chunks absent from `knowledge`. Preserve the 40 unexplained cases for investigation; do not equate all absent vectors with lost production content.
3. Build into a fresh physical collection using real embeddings. Reserve memory/CPU for this job and pause conflicting work only through the release owner's agreed maintenance plan.
4. Check identity, expected IDs/counts, source/version consistency, vector sanity and representative retrieval. Count equality alone is insufficient.
5. Run the V1 end-to-end acceptance suite against the replacement; catch up writes using a reviewed pause or replay strategy; switch the alias and smoke-test it.
6. Retain the old collection and backup until rollback expiry. Cleanup is a separate reviewed step.

Exit: every served point maps to an intended current source, no synthetic/padded test vectors remain in the serving corpus, and rollback restores the previous service configuration.

### R08 — Replace release-score shortcuts with a real V1 gate

Scope: evaluation runners, dataset manifests, acceptance tests and release reporting.

- Rename the current Q9 result as an **oracle verification benchmark** and keep it for cheap regressions.
- Freeze dev and holdout by source-document family. Run actual ingestion → embedding → retrieval → reranking → generation → citation verification.
- Record real answer status, correctness, citation coverage and validity, EvidenceRecall@20, correct abstention, latency and failures. A refusal is not a successful answer to an answerable question; empty citation sets cannot satisfy answer coverage.
- Use the plan's existing targets: grounded answer success ≥90%, factual precision ≥98%, citation validity 100%, EvidenceRecall@20 ≥95%, correct abstention ≥95%. Report denominators, sample sizes, per-slice results and uncertainty; do not claim a target based on a tiny sample or unrun metric.
- Re-run the five specified acceptance flows: golden path, workspace isolation, deletion propagation, fabricated-citation rejection, and EN/RO/PT cross-language behavior.
- Keep all test-created PostgreSQL rows, objects, vectors and graphs in dedicated test scopes with cleanup. Production should not accumulate benchmark fixtures.

Exit: all required metrics and acceptance flows have fresh evidence for the deployed candidate commit; missing dependencies or missing measurements block the release.

### R09 — Close V1 scope gaps

For multilingual answers, preserve original quotations and citation spans while adding a conservatively verified translation path; test negation, quantities, attribution, paraphrase and unsupported translations. If this is deferred, explicitly narrow the V1 release scope rather than marking acceptance #5 complete.

For connectors, connect the existing consumer to workspace-authorized configuration, scheduling, secret references, cursor persistence and operational status. Test repeated sync, changed content, deleted sources, restart recovery and denied-workspace access. If deferred, label V1 upload-only and M6 pending.

## Phase 2 — Make V2 an integrated, measurable feature

| Task | Implementation | Required proof |
|---|---|---|
| **R10 — reliable graph queue** | Propagate enqueue failures; require a durable receipt; register `GRAPH_ENQUEUE_FAILED`; keep payload/queue publication atomic or recoverably reconciled | Broker failure cannot produce a false enqueue-success log or silently finish graph work; replay is idempotent; taxonomy test passes |
| **R11 — graph deployment** | Start a supervised graph consumer; pass graph flags/path to both services; persist a shared graph store; expose per-workspace enablement and queue/heartbeat status | Upload via the real API produces graph rows visible to a separate API process; restart/retry/delete/supersession and workspace isolation work |
| **R12 — mode correctness** | Ensure declared extraction modes select their actual implementation; current `process_graph_job` always constructs `RuleBasedGraphExtractor` | `rule` and `llm` select documented paths or unsupported mode is rejected; no silent substitution |
| **R13 — paired evaluation** | Use equal corpus, top-K, query set and declared compute budgets with graph off/on; persist real answer/status/citation results | Report Recall@20 and correctly answered/cited questions, failures and paired confidence intervals; no graph benefit claimed from refusal-text overlap |

Exit: graph-off behavior retains V1 parity, graph-on runs through the deployed consumer/store path, and evidence supports the chosen release claim. Keep graph disabled if it is operationally correct but offers no demonstrated benefit for its intended questions.

## Phase 3 — Complete V3 without recreating the existing components

**R14 — real factories and shared policy:** use the existing model router/provider and scoped knowledge search/read tools. Derive allowed models/tools and budget caps from server-side policy; requests may request limits but cannot enlarge organizational policy.

**R15 — durable lifecycle:** create a durable queued run and return its ID before execution. Connect the existing queue/worker, checkpoint, step and evidence interfaces to persistent stores; implement status, bounded event history, cancellation, approval and resume against that authority. Checkpoint-write failure must be visible and cannot be treated as durable success.

**R16 — real research pipeline:** wire planner → bounded fan-out → original-source evidence → verification → gap/contradiction handling → synthesis. Revalidate source deletion, version changes and membership revocation at resume and report read/export; make no-evidence, partial and failed states explicit.

**R17 — user surface and rollout:** rebuild the frontend from the same candidate commit only after the backend path is real. Show actual run output, persisted steps, budgets, sources and limitations; ensure cancel/approval actions correspond to backend transitions.

Exit: a real task uses an actual authorized tool/provider and produces correct output; a real research question cites current stored chunks; an empty workspace never generates a fake report. Two API processes can read the same run, a worker can recover after interruption without duplicating effects or resetting budgets, and revoked access blocks resume/read as required. Run one staging smoke with the default production factory and **no test factory injection**.

## Phase 4 — Finish V4 on the working V3 execution path

**R18 — durable revisions:** implement the draft-store backend and a real migration. Enforce append-only history and concurrent revision allocation; verify stored content hashes on read and bind approval to workspace, revision hash and policy version. Correct the draft SQL's claims before adoption.

**R19 — API and UI:** implement authorized draft/import/validate/save/approve/run/status/cancel endpoints from the existing schemas. Deliver a minimal visual authoring surface that only offers supported nodes and shows validation errors before execution.

**R20 — real runtime integration:** supply a production V3 runner and capability manifest to the workflow adapter. Keep fake executors out of production dependency injection. Persist run/checkpoint history and propagate budget, cancellation and evidence failures accurately.

Exit: create → save immutable revision → approve that hash → execute → inspect real output works through the UI. Editing creates a new unapproved revision; restart resumes correctly; unsupported code imports fail; cross-workspace access fails; disabled flags cause zero dispatch. Do not describe library deployment with execution disabled as a completed workflow product.

## Release discipline across every phase

Each candidate must pass backend unit/contract checks, meaningful integration tests, Ruff, Mypy, the adapter import guard, frontend tests/typecheck and a clean production build. Correct the current three vendor-import guard hits at their boundaries instead of weakening the guard. Fix the graph taxonomy contract and current long test line.

Build API, worker and web from an immutable commit; include the commit in image labels and `/version`; keep production free of source mounts and auto-reload. Store the test report, dataset hashes, configuration feature flags and rollback target with the release record. Update completion status from that evidence rather than from ticket counts.

The immediate next work package is **R01–R03**, followed by **R05–R07**. V4 work should wait until real V3 execution is accepted; a large green component test count does not remove that dependency.
