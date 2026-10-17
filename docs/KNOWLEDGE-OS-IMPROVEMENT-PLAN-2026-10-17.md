# Knowledge OS improvement plan — 2026-10-17

Recommendation: finish an honestly measured, reliable V1; repair V2 graph provenance and lifecycle; enable graph retrieval only after paired live evaluation. Preserve V3/V4 and contain their current correctness defects, but defer new agent/workflow capabilities until both product acceptances. Keep the current stack and one retrieval framework, LlamaIndex.

This incorporates [the review of Muse's feedback](../reviews/2026-10-17-muse-plan-review.md). It is a proposed execution plan for the coordinator, not a change to task status or deployment authorization.

## Basis and review limits

Local source baseline: `c46927c85119a915fa575369371a37b7c35d31ad`, plus explicitly preserved active benchmark edits. Read the controlling PLAN/TASKS, DEVELOPMENT-LOOP, V3-V4-DELIVERY-PLAN, REUSE_MAP and LICENSES, existing acceptance evidence, and both October 17 reviews.

For all ten upstream projects, retrieved GitHub metadata, the default-branch commit, README and root license. Inspected selected source/docs for relevant seams, listed at immutable URLs in [the evidence ledger](research/2026-10-17-rag-repositories.json). FastGPT received a documentation/license assessment only because its additional restrictions exclude immediate source adoption. This is a scoped architecture and reuse review, not a full code/security audit of ten platforms. Upstream performance and production-readiness claims were not independently benchmarked.

## What to take from each repository

| Repository | Observed useful capability | Knowledge OS decision |
|---|---|---|
| [RAGFlow](https://github.com/infiniflow/ragflow) | Parsing-result inspection, source-linked chunks and a separate retrieval testing screen; Apache-2.0. Current tree includes a Go backend. | **Q4/Q6/Q10 product patterns.** Add source/page inspection and show retrieval candidates before answer generation. Keep Docling and the existing API; do not import a second platform or allow editing indexed text without creating canonical versions. |
| [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm) | Workspace-oriented local/cloud configuration, source citations and document UX; MIT. Telemetry setup has an explicit disable switch. | **Q6 local/privacy UX patterns.** Display the effective processing route and local-only limitations per workspace. Enforce policy server-side for embedding, rerank, generation and graph calls; self-hosting alone does not establish no egress. |
| [LlamaIndex](https://github.com/run-llama/llama_index) | Transform hashing/cache and document-store lifecycle in ingestion; parent-node merging in retrieval; MIT. | **Primary component reuse.** Keep the existing splitter adapter; test cache invalidation and parent/child retrieval as later measured improvements. Scope caches by workspace, version, parser/chunker configuration and model identity. Preserve canonical IDs and ACL checks when expanding to parents. |
| [LightRAG](https://github.com/HKUDS/LightRAG) | Source-ID merging, deletion with remaining-source handling, and graph rebuilding from chunks; MIT. | **Highest-value V2 reference.** Repair contribution storage and deletion semantics in the existing graph adapter. Study the implementation; do not install a parallel graph RAG runtime. Retain canonical source resolution and lifecycle fences. |
| [Quivr](https://github.com/The-Vibe-Company/quivr) | Current V2 documents committed ingestion receipts, idempotent writes, rebuildable projections and explicit dependency failures; MIT. | **Q0/Q1/Q3/Q7 contract patterns.** Durable acceptance, idempotency keys and degraded-mode disclosure fit existing PostgreSQL/Qdrant/Redis. Do not migrate to its Go/Temporal/Weaviate stack. Its README explicitly says evaluation-stage and not production-ready. |
| [Microsoft GraphRAG](https://github.com/microsoft/graphrag) | Community/global search with budgeted map/reduce; MIT. | **Later V2 experiment only.** Try corpus-wide summary queries if labelled questions justify them. Keep citations tied to active canonical evidence. Upstream says largely maintenance mode; do not make its full indexing runtime a new foundational dependency. |
| [FastGPT](https://github.com/labring/FastGPT) | Knowledge-base QA and visual workflow product design. Root license adds multi-tenant SaaS and branding restrictions to Apache terms. | **No source adoption under current policy.** Record desired workflow capabilities as product requirements; retain the existing Langflow import boundary for scheduled V4. Do not copy UI/code or infer permissive licensing from an Apache reference. |
| [Haystack](https://github.com/deepset-ai/haystack) | Explicit evaluator comparison keys, single/multiple-hit recall, empty-input handling and opt-in content tracing; Apache-2.0. | **Q2/Q8/Q9 evaluation/trace patterns.** Define canonical evidence recall and safe spans in existing modules. Do not add Haystack as a second retrieval framework. Its document recall definition is a reference, not a substitute for the agreed evidence metric. |
| [DocsGPT](https://github.com/arc53/DocsGPT) | Source-based retrieval, bounded source fan-out, source-search spans and self-hosted assistant/API UX; MIT. | **Q6/Q7 source and API patterns.** Expose source status, bounded retrieval work and evidence details. Defer new widgets, connectors and agent capabilities until acceptance. Preserve Knowledge OS workspace permissions. |
| [R2R](https://github.com/SciPhi-AI/R2R) | Versioned document APIs, asynchronous ingestion, collection association and retrieval interfaces; MIT. | **API contract reference.** Make accepted job IDs, lifecycle states and error outcomes consistent. Keep FastAPI and current stores. GitHub's observed last push is November 7, 2025: verify release/support cadence before considering a runtime dependency. |

The ledger records exact star counts as a dated snapshot. Popularity does not determine suitability. Quivr's old owner redirects to The-Vibe-Company. LlamaIndex's current README describes a company focus shift toward parsing/extraction; retain bounded, pinned framework components and an adapter escape path. None of these observations alone establishes abandonment or safety.

Root licenses are a screening step. Any future copied code needs exact file/dependency license review, pinned revision, attribution and REUSE_MAP/NOTICES entries. FastGPT's [actual license](https://github.com/labring/FastGPT/blob/ce153e1b8c998ffa8b2bf8d8eaa7bca62f651249/LICENSE) has additional restrictions; no permission request is needed to complete this research, because no adoption is proposed. Model approval is a separate gate from repository licensing.

## Phase 0 — reconcile the release candidate

**Owner:** coordinator with benchmark owners. **Dependency:** none.

- Keep the draft branch/PR; record reviewed code SHA, deployment SHA, corpus hashes and environment/model identities separately. Preserve active benchmark files and commit only owner-reviewed artifacts; use an isolated checkout for acceptance if the shared tree remains active.
- Reconcile open findings with source. Workflow paths and lexical lifecycle predicates are already wired. T-V1-080's delete error precedence is already implemented. Retain their release regressions instead of assigning duplicate fixes.
- Inventory current H1/H2 scoring before writing another runner. Freeze one metric contract mapping each release target to an executable command, denominator, corpus and artifact.
- Record provider/model/revision/dimension and actual vector collection/alias. Do not treat old bge-m3/1024-d design text and current Gemini/3072-d configuration as interchangeable.

**Exit:** one immutable candidate manifest, an accurate blocker list and named owners. No reversion of unrelated benchmark work, automatic corpus deletion, service restart or acceptance declaration.

## Phase 1 — correctness, containment and green engineering gates

| Package | Implementation boundary | Acceptance evidence |
|---|---|---|
| Durable admission | `docker-compose.yml`, `.env.example`, API/worker startup, `packages/stores/agent_stores.py`, lifecycle routes | When enabled, API and independently started worker use the same durable store; accepted runs survive restart. Missing/unwritable configuration rejects admission explicitly. Disabled capabilities cannot accept work. Test evidence/checkpoint persistence separately; changing one DB variable does not prove it. |
| Queue ownership | SQLite queue adapter and lease operations | Begin write transaction before selecting; unique claims; token-conditioned mutations with row-count checks. Two fresh worker processes race on one ready run; only one owns it. Expiry/recovery, old-token ack/renew/park, crash-before-ack and idempotent replay are exercised. |
| Search isolation | `apps/web/src/app/search/page.tsx` and request contract | Key state to workspace or invalidate requests on switch; guard success, error and finally. A delayed A result/error never appears under B or clears B's loading state. Backend authorization tests remain separate. |
| Verification honesty | `packages/workflow_engine/production_executor.py`, shared answer verification boundary | Configuration alone never produces a verified badge. Missing/foreign/deleted evidence and verifier failure produce explicit unverified/unavailable outcomes. Immediate containment does not require completing V4. |
| Graph lifecycle | `apps/worker/jobs/graph_extraction.py`, `packages/graph/sqlite_store.py` | Contributions keyed by workspace/document/version/chunk; aggregates retain surviving support. Delete/supersede while extraction is paused, then resume: obsolete contributions stay invisible. Test both ingestion orders, replay and shared-entity deletion. Keep graph mode off. |
| Setup and CI | README/env examples, adapter imports, typing/lint, CI workflow | Documented configuration resolves with safe test values; actual configured lint/type/policy/test/build jobs pass on the candidate SHA. Do not weaken model or adapter rules to obtain green checks. |

A graph publication design must cover the PostgreSQL/SQLite boundary: use durable lifecycle generations/tombstones and a coordinated publication protocol, or a durable outbox with consumer fencing and canonical read checks. Demonstrate its race semantics before selecting a broader store migration. Physical purge can retry after logical deletion, but deleted data must become ineligible immediately. Test orphan rows too: the present lexical compatibility predicate permits missing canonical rows and must be assessed against the intended lifecycle contract.

**Exit:** reproduced defects have regression tests and reviewed fixes/containment; required engineering gates green. These repairs do not establish live answer quality.

## Phase 2 — V1 quality and operability

**Owner:** dev1, with dev2 preparing independent V2 fixtures/contracts. Use existing Q cards rather than a second competing queue.

1. **Q0/Q1/Q3:** align error taxonomy, durable acceptance and generation-fenced publication. Ready means verified durable writes with real vectors; partial extraction is disclosed. Dependency outages must not masquerade as empty successful retrieval. If lexical-only degraded service is allowed, expose the effective mode explicitly and test it.
2. **Q4/Q6:** build a format truth table for PDF/scans/DOCX/XLSX/images and multilingual content. Show extracted passages, page/bbox where available, omitted regions and actionable failure reasons. Do not advertise a format based on a parser library's capability alone. Use RAGFlow's inspection idea while keeping the current Docling adapter.
3. **Q5:** share canonical evidence resolution and publication decisions across answer surfaces. Evaluate complete factual prose, not just citation syntax. Unsupported numeric/table claims, wrong source/version, foreign workspace and deleted evidence must fail the appropriate gate.
4. **Q2/Q7:** propagate trace IDs from ingress through worker, parsing, embedding, retrieval, rerank and verification. Record safe status/model/generation/count/latency/cost fields; leave raw content off by default. Distinguish process liveness from dependency and enabled-feature readiness. Expose build identity and test backing-store failure/recovery.
5. **Q8/Q9:** prepare real labelled EN/RO/PT, table, multi-source and unanswerable slices concurrently with fixes; run the actual upload → originals → parse → embed → Qdrant/FTS → retrieve/rerank → generate → verify path. Use isolated PostgreSQL, vector, object-store and queue namespaces, not only isolated Qdrant collections. Clean only run-owned artifacts and compare pre/post inventory.
6. **Q10:** on a separate development set, change one factor at a time: chunking, parent expansion, dense/lexical fusion, candidate K, reranker, then optional query expansion. Promote only improvements supported by paired quality/latency/cost results. Keep a frozen holdout untouched by tuning.

### Release metric contract

| Target | Measurement and safeguards |
|---|---|
| Grounded success ≥90% | Per eligible query: answer satisfies the task and its factual claims are supported by canonical evidence. Record answerable coverage separately so refusal cannot inflate quality. |
| Factual precision ≥98% | Supported/correct factual claims divided by evaluated factual claims, with adjudicated gold labels and calibrated judges where used. Report claim and query denominators and sampling uncertainty. Answer EM/F1 or token overlap alone is insufficient. |
| Citation validity 100% observed | Every emitted citation resolves to authorized, live document/version/chunk and its claimed locator. Separately require citations for claims that need evidence. An answer needing evidence with zero citations fails; an appropriate refusal is N/A for citation validity, not a free success. |
| EvidenceRecall@20 ≥95% | Top 20 eligible retrieved evidence units versus gold canonical evidence, with the retrieval stage and matching unit frozen. Report all-required-evidence coverage for multi-hop questions. H2's title Recall@10 does not meet this contract. |
| Correct abstention ≥95% | Correct refusals on genuinely unanswerable questions; separately report false refusals on answerable questions. Dependency errors are errors, not correct abstentions. |

Report overall and slice counts/scores, confidence intervals and failed cases. Use paired bootstrap at the query level for configuration comparisons; do not treat correlated claims from one question as independent samples. A 50–100-question set is a pilot. Select the final sample size from the required precision and slice coverage; small slices with wide intervals remain inconclusive. Store data hashes, model/version/prompts, candidate SHA, runtime identity, exact commands and raw scored outputs. Budget live model calls explicitly before execution.

**Exit:** full configured unit/contract/integration and web gates pass on the candidate, and a frozen live Q9 report measures all five targets. Historical 227 frontend passes or 3,096 collected tests are evidence about an earlier snapshot, not permanent required counts. Coordinator accepts V1 based on current results.

## Phase 3 — measured V2 integration

Before Q9, complete graph contribution/lifecycle repairs, fixtures, documentation and contracts within the approved graph seams. After live Q9 and coordinator dispatch, wire G4 workspace/global toggles, Ask/Search mode and traversal debugger. Preserve the `graph_retriever=None` baseline and mode-off output parity; enforce ACL before traversal and canonical hydration before citation.

G5 compares graph versus baseline on the same frozen multi-hop questions, K, model, prompt, token/latency/cost budgets and corpus revision. Record evidence recall, all-hop coverage, grounded success, false refusals, latency and cost, with paired confidence intervals. Include repeated entity names, competing versions, delete/reingest and tenant isolation. Require a predeclared useful improvement without unacceptable regressions. If the result is inconclusive, leave graph opt-in/off; synthetic tests cannot justify a quality claim.

Use LightRAG's contribution/rebuild patterns first. Evaluate Microsoft-style community/global summaries only for a labelled corpus-wide question class, with separate indexing cost and provenance checks. Rebuildable graph projections must never become the canonical source of truth.

**Exit:** G4/G5 evidence reviewed and V2 explicitly accepted.

## Phase 4 — scheduled V3/V4

Only after both acceptances and dispatch: bounded LangGraph spike behind adapters, durable run/step/checkpoint/evidence storage, server-enforced tools and approval, then source verification and synthesis. Build workflow execution on the accepted V3 runner, with immutable hashed revisions, authorized import/approve/run/cancel and real production execution. Preserve W0/W1 and the restricted Langflow adapter. Implement and test disabled-by-default feature admission; do not assume proposed environment flags already enforce it.

Do not add a second full RAG platform, Haystack runtime, a new vector database, broad connector catalog, visual workflow editor or autonomous research subsystem to the V1 critical path.

## Delivery and validation

Each package is a scoped commit/PR with a failing reproduction, fix, regression evidence, migration/rollback note where applicable and coordinator handoff. Keep the current PR draft until its actual gates and acceptance scope are satisfied. Estimate effort after Phase 0 identifies corpus readiness, isolated environment costs and ownership; Muse's dates remain provisional.

This deliverable changes documentation only. Validation: source and history inspection, current GitHub check inspection, ten repository metadata/license snapshots, selected upstream source/doc reads, and local link/JSON/diff checks. No upstream code was executed or copied into the product; no new live evaluation or deployment was performed.

TASK: Review ten RAG repositories and prepare a Knowledge OS improvement plan, incorporating Muse's feedback.
CHANGE: Prioritized plan, corrected feedback review and pinned upstream evidence ledger.
WHY: Close correctness and measurement gaps before expanding features; follow existing reuse and V1/V2 sequencing rules.
REUSED: Existing Knowledge OS stack; upstream references only, no new dependencies.
BUILT: Planning and review documentation only.
VERIFY: Evidence ledger and linked reviews; documentation integrity checks.
RISKS: Open product defects, incomplete live acceptance, upstream license/support caveats; coordinator retains merge/deploy authority.
