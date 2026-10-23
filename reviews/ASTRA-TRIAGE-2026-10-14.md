# Triage — ASTRA V1–V4 review (museR, 2026-10-14)

Method: every cited mechanism re-read in-tree before adjudication.
Result: 22/22 CONFIRMED (4 High, 17 Med, 1 Low). All four REJECT verdicts SUSTAINED.
Severity notes where the finding is real but narrower than stated.

## Highs (drive the verdicts)

| ID | Claim | Adjudication |
|---|---|---|
| V1-01 | Sensitive-workspace queries egress to cloud embeddings; generation transport is local-only but retrieval hardwires `OpenRouterProvider` (`answer/factory.py`, `retrieval/orchestrator.py:303`); `build_answer_engine` takes only `model` | CONFIRMED High. The factory docstring even claims no-external-gateway, which the retrieval path contradicts. Conditional on cloud embedding key; in this deployment ALL Ask queries egress by design, so local-only workspaces have no retrieval boundary at all. |
| V2-01 | Accepted queued agent runs (202) never consumed: worker entrypoint starts ingest + optional graph only; zero non-test `AgentLifecycleWorker(` instantiations | CONFIRMED High. `run_worker` = ingest jobs; agent loop has no caller in apps/packages/scripts/compose. |
| V2-02 | Production research verifies against `InMemoryChunkSource` (docstring: for tests); tool outputs `put` into it (`research_pipeline.py:264,283`), verifier reads it back; no authoritative re-check, no deletion/version notifications | CONFIRMED High. Deleted/replaced chunks can still ground claims. |
| V4-01 | API drafts/approvals/runs/checkpoints all in-memory (`workflows/store.py` dicts + `InMemoryDraftStore`; per-run `InMemoryCheckpointStore` at `router.py:655`); SQLite classes exist but no production injection found | CONFIRMED High. Restart loses data; multi-process disagree. Urgency softened (testing-only deployment, user-directed) but finding is true. |

## Meds / Low (all confirmed; notes where narrowed)

- V1-02 CONFIRMED Med: `query_identity` optional, unchecked when absent; orchestrator never passes it. Needs same-width foreign-model collection to bite.
- V1-03 CONFIRMED Med: catch-all → `{}` → reported as missing evidence. Narrowed: a warning IS logged; content stays closed. Defect = status honesty, not leakage.
- V2-03 CONFIRMED Med: `_checkpoint` loaded at `agent_worker.py:116` never reaches `runtime.run_enforced`. Moot until V2-01 lands (no consumer), then load-bearing.
- V2-04 CONFIRMED Med: `sorted()` IDs, uniform 1.0/0.9 scores, substring community match; local+global concat can double-vote in RRF. Causal link to table-QA −0.081 is plausible explanation, NOT proven (reviewer states this honestly).
- V2-05 CONFIRMED Med: per-chunk error records → log-and-continue → success counts + payload delete. Total-provider-failure-as-success real.
- V2-06 CONFIRMED Med: unconditional ack after handler exception (`graph_extraction.py`, "for now always ack"); stranded-job outcome requires a silent retry-write failure inside handle — narrower than stated, still real.
- V2-07 CONFIRMED Med: flag-off → `None` → `_graph_leg` returns `[]`; explicit graph/graph-only requests indistinguishable from empty success.
- V3-01 CONFIRMED Med: zero app callers of `resolve_node_executor`; API constructs `ProductionNodeExecutor` directly. Flag does nothing on the API path. Impact capped (flag-off default is safe/same).
- V3-02 CONFIRMED Med: adapter returns runner result unvalidated. Latent (adapter disconnected from API per V3-01).
- V3-03 CONFIRMED Med: approve only re-queues; runtime `approve()` never called. Doubly inert with V2-01.
- V3-04 CONFIRMED Med: authoring manifest (all NodeType + `document_read`/`llm_generate`) vs production manifest (narrower + `knowledge_read`/`llm`). `valid`/`approved` for unexecutable flows; loud runtime refusal caps impact at UX/contract.
- V4-02 CONFIRMED Med: `await execute` THEN `save_run`; no live run ID, cancel reaches only registered executors. Lifecycle test proves terminal-cancel only.
- V4-03 CONFIRMED Med: `except Exception: return {"output": template}` — literal placeholders as COMPLETED success.
- V4-04 CONFIRMED Med: durable serialization truncates (8k) + redacts; in-memory keeps originals → divergent replay values.
- V4-05 CONFIRMED Med: checkpoint failures counted on ephemeral executor, never surfaced; resume-without-checkpoint starts fresh silently.
- V4-06 CONFIRMED Med: resume seeds ledger from caller-supplied new input; no input-vs-checkpoint comparison. (No resume endpoint exposed — component-level.)
- V4-07 CONFIRMED Med: sequence alloc + `INSERT OR REPLACE` in separate connections; multi-process collision destroys state. Single-process safe.
- V4-08 CONFIRMED Low: `!=` vs NULL never aborts; direct-DB-writer-only gap. Store supplies values normally.

## Repair wave filed (T-A01..T-A21, all QUEUED, none dispatched)

Highs: T-A01 (V1-01), T-A02 (V2-01), T-A03 (V2-02), T-A04 (V4-01).
Meds: T-A05 (V1-02), T-A06 (V1-03), T-A07 (V2-03), T-A08 (V2-04), T-A09 (V2-05),
T-A10 (V2-06), T-A11 (V2-07), T-A12 (V3-01), T-A13 (V3-02), T-A14 (V3-03),
T-A15 (V3-04), T-A16 (V4-02), T-A17 (V4-03), T-A18 (V4-04), T-A19 (V4-05),
T-A20 (V4-06 + V4-07 same-file family, two criteria), T-A21 (V4-08 migration NULL-safety).
Dependency notes: T-A07 moot until T-A02; T-A13 latent until T-A12; V2-03/V3-03 cluster around the unconsumed agent path.
K's pending review may duplicate: dedup on landing, no double-build.

## Standing

- No finding was rejected; nothing sent back to astra. Her file stays as the evidence source; this file is the adjudication.
- Dispatch stance: repair wave waits behind R13b + K dedup. No lane starts early.
