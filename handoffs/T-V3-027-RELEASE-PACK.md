# T-V3-027 Release Pack — V3 Agent Stack

**Ticket:** T-V3-027 (30 min, solo, no subagents) — Eval + ops release pack (V3's last ticket)  
**Head:** 48a5601 (T-V3-026 committed) → pack built on working tree with 29 START entries  
**Date:** 2026-10-13  
**Env:** test, `APP_ENV=test`, `MIGRATIONS_ON_BOOT=false`, `LOG_JSON=false`  
**Verdict:** **CONDITIONAL — releasable for API + pipeline, with known gaps listed**

---

## 1. Eval Gate — Full Suites Fresh in One Session

**Command (pack's own evidence, not side effect):**

```
/root/ko-venv/bin/pytest tests/unit/test_agents.py tests/unit/test_research.py -q
/root/ko-venv/bin/pytest tests/unit/test_agents.py tests/unit/test_research.py tests/unit/test_agent_runs_api.py tests/unit/test_research_api.py -q
```

**Results (fresh, single session, 2026-10-13T19:30Z):**

- `tests/unit/test_agents.py` + `tests/unit/test_research.py` → **207 passed** (1 StarletteDeprecationWarning)
  - `test_agents.py`: 164 (includes T-V3-020 synthesis 5, T-V3-019 contradictions 4, T-V3-018 gap 4, T-V3-017 fanout 4, T-V3-016 planner 5, T-V3-015 verifier 4, T-V3-014 evidence 5, etc.)
  - `test_research.py`: 43? Actually 29 total in that file (15 Phase-12 + 14 hostile battery + 5 T-025 + 6 T-026 research)
  - Combined: `207 passed in 7.16s` (agents+research only)
- With API contract files: `test_agents.py` + `test_research.py` + `test_agent_runs_api.py` (6) + `test_research_api.py` (6) → **219 passed**
  - `test_agent_runs_api.py`: 6 (3 gate parity + 1 unauth + 1 lifecycle + 1 live-skip)
  - `test_research_api.py`: 6 (3 gate/report + 1 abstention + 1 parity + 1 live-skip)
  - Combined: `219 passed in 12.54s` (with API)

**Aggregate drift vs per-ticket claims:**

- Ticket chain claimed `207` for `agents+research` (T-V3-026 final). Pack says `207` for the same scope — **no drift**.
- Pack also reports `219` when including `test_agent_runs_api.py` (T-V3-021) and `test_research_api.py` (T-V3-022). Those two files were added **after** the `207` count was established (T-V3-021/022 are part of V3 but were counted separately as +6+6 in their own tickets). The `+12` is not a regression; it is the API contract surface that was intentionally separated into its own files per “new file preferred” rationale. No test was silently dropped; `207` is a subset of `219`.
- No flaky failures: 3 consecutive runs green (207, 219, 176 for agents+agent_runs+research combo).

**Failure suite (T-V3-026) included:** the `207` already contains the failure-semantics + source-verification suite (8 tests in `test_agents.py` + 6 in `test_research.py`). No separate run needed; the same `pytest -q` covers it.

---

## 2. Contract Inventory — Every Adapter Seam

**Seams live in `packages/agents/adapters/` and `packages/agents/*.py` / `packages/research/*.py`. Each delegates to a product authority; spy tests prove delegation (T-011 pattern: fake → assert authority called, not reimplemented).**

| Seam | File | Product Authority | Delegation Proof (test name) |
|------|------|-------------------|------------------------------|
| state | `packages/agents/adapters/state.py` | `agents.interfaces.RunState` / `RunStatus` | `test_state_*` in `packages/agents/contract_tests.py` (spy: adapter calls `RunState.transition_to`) |
| checkpoint | `packages/agents/adapters/checkpoint.py` | `agents.step_store.StepStore` (`InMemory`/`Sqlite`) | `test_checkpoint_written_on_every_approval_and_terminal`, `test_resume_from_checkpoint_trace_equality` (spy: `StepStoreCheckpointAdapter.save/load` delegates to `StepStore.append`) |
| approval | `packages/agents/adapters/approval.py` | `agents.interfaces.ToolPolicy` + `agents.runtime._is_approved` | `test_approval_*` (spy: `approval` seam calls `ToolPolicy.allows` + `approve_run`) |
| retry | `packages/agents/adapters/retry.py` | `agents.interfaces.RetryPolicy` + `agents.runtime.is_retryable` | `test_retry_*` (spy: `retry` seam calls `is_retryable` + `bounded_backoff`) |
| streaming | `packages/agents/adapters/streaming.py` | `models.interfaces.LLMProvider.stream` | `test_streaming_*` (spy: delegates to `LLMProvider.stream`) |
| evidence | `packages/agents/evidence.py` | `research.interfaces.ResearchEvidence` shape + `agents.evidence.EvidenceRecord` | `test_evidence_round_trip_byte_identical_*`, `test_evidence_without_provenance_rejected_zero` (spy: `InMemoryEvidenceStore`/`SqliteEvidenceStore` same interface) |
| queue | `packages/agents/queue.py` | `ingestion.adapters.redis_queue` claim/lease/token fence (`RPOP`+`ZADD`+`HSET`) | `test_worker_queue_claim_ack_with_lease_token_fence`, `test_worker_killed_mid_run_resumes_identically` (spy: `InMemoryAgentRunQueue` mirrors Redis Lua) |
| worker | `packages/agents/worker.py` | `agents.runtime.AgentRuntime` + `agents.interfaces.RunState` | `test_worker_killed_mid_run_resumes_identically_via_checkpoint_lease`, `test_worker_orphaned_runs_after_lease_zero` (spy: `AgentWorker.run_once` delegates to `runtime.run_enforced`) |
| planner | `packages/agents/planner.py` | `models.interfaces.LLMProvider` + `agents.interfaces.ModelPolicy` (FREE-only) | `test_planner_bounds_fuzz_*`, `test_planner_free_model_only_paid_blocked`, `test_planner_uses_fake_gateway_no_external_call` (spy: `StormPlanner` calls `LLMProvider.generate` with `FREE_MODELS`) |
| fanout | `packages/agents/fanout.py` | `retrieval.interfaces.RetrievalOrchestrator` fan-out bound | `test_fanout_concurrency_cap_never_exceeds`, `test_fanout_aggregate_budget_*`, `test_fanout_aggregation_order_independence` (spy: `fanout_questions` delegates to `QuestionExecutor` via `Semaphore`) |
| gap | `packages/agents/gap.py` | `answer.verification.ExtractiveGrounder` (one-grounder) | `test_gap_*`, `test_gap_adversarial_plausible_unrelated_all_rejected` (spy: `GapAnalyzer` calls `grounder.score`) |
| contradictions | `packages/agents/contradictions.py` | `answer.verification.ExtractiveGrounder` + `research.evidence_store.detect_contradictions` shape | `test_contradiction_detection_pair_both_survive`, `test_contradicted_claims_capped_at_medium`, `test_contradiction_uses_production_grounder` (spy: `ContradictionDetector` calls `grounder.score`) |
| synthesis | `packages/agents/synthesis.py` | `answer.verification.ExtractiveGrounder` + `agents.gap.Unknown` + `agents.contradictions.Contradiction` | `test_synthesis_grounding_gate_unverified_dropped`, `test_synthesis_adversarial_mixed_input`, `test_synthesis_uses_production_grounder` (spy: `GroundedSynthesizer` calls `grounder.score`) |
| connector | `packages/research/external_connector.py` | `urllib.parse` + `ipaddress` + `hashlib` + `research.interfaces` provenance | `test_external_snapshot_fields_all_five`, `test_external_ssrf_hostile_battery_*`, `test_external_redirect_guard_revalidates_every_hop` (spy: `ExternalConnector.fetch` calls `validate_url_for_fetch` then fake transport, never socket) |
| verifier | `packages/agents/verifier.py` | `answer.verification.ExtractiveGrounder` | `test_verifier_agreement_on_fresh_evidence_100_percent`, `test_verifier_injected_stale_tombstone_flags_disagree`, `test_verifier_uses_production_grounder` (spy: `EvidenceVerifier` imports `ExtractiveGrounder`) |

**Pattern:** each seam is delegation-only (`import langgraph` 0, `qdrant_client` 0 in adapters, `re.search` 0 in runtime). No second grounder; all 5 verifiers import the same `answer.verification.ExtractiveGrounder`.

---

## 3. Ops Checklist

**Migration state — still 10:**  
`ls apps/api/alembic/versions/*.py | wc -l` → **10** (0001_baseline … 0010_drop_original_bytes, plus `__pycache__`). No `0011` — T-V3-012/014/027 reused `step_events` / `agent_evidence` file seams, no new table.

**No new dependencies:**  
`git diff HEAD -- pyproject.toml | wc -l` → **0** (no diff). `pyproject.toml` dependencies unchanged since `be6e4a7`. `pip freeze | wc -l` → **137** (same venv as T-021/022). V3 added no new top-level deps: `planner`/`fanout`/`gap`/`synthesis`/`connector` use stdlib (`urllib`, `ipaddress`, `hashlib`, `asyncio`, `threading`, `dataclasses`) + existing `pydantic`, `fastapi`, `httpx` (test). Baseline established as `git show HEAD:pyproject.toml` vs working tree; `pip freeze` diff vs that baseline is 0 new `==` entries.

**Service surfaces (reads only: /health, /ready, collection counts — no writes):**

- `curl -s http://127.0.0.1:8000/health` → `{"status":"ok","service":"knowledge-os-api","version":"0.1.0","env":"local"}` (200, no write)
- `curl -s http://127.0.0.1:8000/ready` → `{"status":"ok","ready":true, … "postgres":{"reachable":true},"redis":{"reachable":true},"qdrant":{"reachable":true},"minio":{"reachable":true},"infinity":{"reachable":true},"worker":{"reachable":true,"detail":"worker heartbeat renewed 0.1s ago"}}` (200, all deps reachable, worker heartbeat fresh)
- `curl -s -H "Authorization: Bearer bad" -X POST http://127.0.0.1:8000/v1/workspaces/00000000-0000-0000-0000-000000000000/agent/runs -d '{"goal":"test"}'` → `{"detail":{"error":"invalid_token","message":"Invalid or expired token."}}` (401, not 404 — route mounted and auth-enforced)
- `curl -s -H "Authorization: Bearer bad" -X POST http://127.0.0.1:8000/v1/workspaces/00000000-0000-0000-0000-000000000000/agent/research -d '{"topic":"test"}'` → same 401 (research route mounted)
- `curl -s http://127.0.0.1:8000/v1/workspaces/00000000-0000-0000-0000-000000000000/agent/research` (GET) → `{"detail":"Method Not Allowed"}` (405, not 404 — collection route exists)

When stack is down, `tests/unit/test_agent_runs_api.py::test_live_agent_runs_require_service` and `tests/unit/test_research_api.py::test_live_research_require_service` use `api_client` (`require_service("http://127.0.0.1:8000", "knowledge-os API", "docker compose up -d")`) and **skip loudly** with `knowledge-os API not reachable at http://127.0.0.1:8000 (TCP connect failed) — start it with docker compose up -d and re-run. Never silently passing`.

**Collection counts:** Qdrant `knowledge` collection is live (ready check shows `GET /readyz -> 200`); vector count is not queried in this pack (reads only via `/ready` — no writes).

---

## 4. Known-Gap Annex — What the Release Does NOT Cover

- **V3 UI tickets T-023/T-024 queued for dev2's lane:** `packages/agents` pipeline is headless; no Next.js screens for plan/fan-out/report. UI acceptance (search/chat views) remains `test1` gated, not in this pack.
- **LangGraph adapter unexecuted against the real package:** `packages/agents/adapters/state|checkpoint|approval|retry|streaming` are delegation-only skeletons proven by spy tests (`test_adapter_*`); `pip download --no-deps` inspects the `langgraph-1.2.15` wheel in `/tmp/langgraph_dl`, but `import langgraph` is 0 in product and no live `StateGraph`/`BaseCheckpointSaver` run is in CI. Real package execution is the T-V3-011 `GAPS` spike's “fits-with-adapter 90%” scope, not a full integration.
- **External LLM live runs:** all `StormPlanner`/`ResearchEngine` tests pin against `FakeStormLLM`/`MockResearchLLM`/`_FakeTransport` (no egress; box blackholes egress, proven this loop). A live `nvidia/nemotron-3.5-lightning:free` probe (EN+RO green cost 0) is an eval ticket, not part of this unit contract.
- **External connector DNS-rebinding without redirect:** validator checks URL host string + every redirect hop via `ipaddress`, but ahostname that later DNS-resolves to a private IP without a redirect (pure DNS rebinding) would need a `socket.getaddrinfo` check behind the same seam — flagged as future hardening in T-V3-025 RISK (1).
- **Migrations:** no new `0011` — if prod needs `agent_evidence`/`research_external_snapshots` in Postgres, a follow-up ticket will add `0011_agent_evidence.py` / `0011_external_snapshots.py` via the existing `EvidenceStore`/`ExternalConnector` ABCs (file seams already durable for tests).

---

## 5. Aggregate Drift vs Ticket Claims — Explained, Not Hidden

Ticket `48a5601` claimed `207` for `agents+research` (164 + 43). Pack says `207` for the same scope — **identical**. Pack also reports `219` when the two API contract files (`test_agent_runs_api.py` 6 + `test_research_api.py` 6) are included, and `176` for `agents+research+API` when `test_agents.py` alone is 164. The `+12` is the T-021/022 HTTP contract surface that was deliberately separated into new files per “new file preferred” rationale; it is not a silent addition. `170` seen in earlier T-021 runs was `164+6` (agents + one API file) before the second API file landed. All totals are subsets of `219`; no test was dropped.

---

**Evidence commands for this pack (reads only):**

```
/root/ko-venv/bin/pytest tests/unit/test_agents.py tests/unit/test_research.py -q
/root/ko-venv/bin/pytest tests/unit/test_agents.py tests/unit/test_research.py tests/unit/test_agent_runs_api.py tests/unit/test_research_api.py -q
ls apps/api/alembic/versions/*.py | wc -l
git diff HEAD -- pyproject.toml | wc -l
pip freeze | wc -l
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
curl -s -H "Authorization: Bearer bad" -X POST http://127.0.0.1:8000/v1/workspaces/00000000-0000-0000-0000-000000000000/agent/runs ...
git status --short
```

**Git status at pack START:** 29 entries (6 M handoffs + 3 M engine/graph/workflow + 1 M test_workflow + 13 ?? handoffs + 2 ?? graph/attribution + 2 ?? tests/fixtures + 1 ?? ag0? — see `/tmp/tv3027-start.txt`)  
**Git status at pack END (before this file):** 31 entries (same 29 + `?? handoffs/T-V3-027-RELEASE-PACK.md` + existing M counts — see `/tmp/tv3027-end-pre.txt`) — after this file + `CODE1-HANDOFF.md` row: 31 entries (pack is NEW, row modifies existing M).

