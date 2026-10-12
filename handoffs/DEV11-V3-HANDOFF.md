# DEV11 — V3 (Agents & Research) Handover

**Developer:** dev11 (`w4:p1A`) · **Reviewer:** rew1 (`w4:p1D`) · **Coordinator:** Antigravity (`w4:p19`)
**Repository:** `/root/code/Super Rag/knowledge-os` · **Branch:** `main` (per dispatcher directive 2026-10-12: all V3 work develops against `main`)
**Plan:** `../rag-fork-solution/V3-AGENTS-RESEARCH-PLAN.md` (card A0)

---

## 0. rew1 REVIEW — A0-1/A0-2/A1: ✅ ACCEPTED (2026-10-12, HEAD `d539f24` on main)

**Verdict:** ACCEPTED, forwarded to Antigravity for final merge. Ledger: `handoffs/REW1-REVIEW-HANDOFF.md` §0c.

All 11 blockers closed and independently verified:
- Gates: 45 passed (3 files), ruff clean, mypy clean (10 files)
- Fail-before-fix demonstrated for all 12 new tests (7 agent + 5 research) against pre-fix code
- rew1's 10 invariant probes pass; security re-scan clean

### Non-blocking follow-ups (from ledger) — ✅ ALL ADDRESSED
1. **research-engine ModelPolicy plumbing** — `ResearchModelPolicy` added (exported); `plan_research`/`synthesize_report` resolve models via `_resolve_model("planning"/"synthesis")` instead of hardcoding. Regression: `test_engine_uses_injected_model_policy`.
2. **Stale `run.error` after COMPLETED** — `run_enforced` clears `run.error` on COMPLETED (both final-answer paths); the blocked step stays recorded in the trace. Regression: step-trace assertions in the 4 dispatch tests.
3. **Filter revoked/deleted ids from abstention `evidence_ids`** — `synthesize_report` excludes revoked/deleted from the verified set *and* from abstention `evidence_ids`, noting the count in `unknowns`. Regression: `test_revoked_evidence_excluded_from_report`.

**Final gate:** 47 passed; ruff clean; mypy clean (10 files).

**Verdict:** REQUEST CHANGES (B1–B11). Full ledger: `handoffs/REW1-REVIEW-HANDOFF.md`.

| Finding | Severity | Status |
|---|---|---|
| B1 PythonSandboxTool RCE | CRITICAL | ✅ **ACCEPTED by rew1** (commit `5c899ec`): zero refs in packages/tests/apps; no eval/exec/subprocess/importlib surface; 23 passed + 10 research contracts; ruff+mypy clean. |
| B11 committed to wrong branch | PROCESS | ✅ **CLOSED by rew1** (main `3dbe8fb`); handoff branch claim corrected. |
| B2 ToolPolicy never enforced | HIGH | ✅ **FIXED** (`fc5b18b`): `run_enforced` checks policy before dispatch; default-deny; BLOCKED + TOOL_NOT_ALLOWED. |
| B3 no ToolSpec validation / reserved-context guard | HIGH | ✅ **FIXED** (`fc5b18b`): strict schema validation, `validate_tool_arguments()`, timeout + output-byte bounds. |
| B4 RunBudget/RetryPolicy/ModelPolicy unwired | HIGH | ✅ **FIXED** (`fc5b18b`): deadline/token/tool-call budget checked before spend; limit states. |
| B5 silent fallbacks → false COMPLETED | HIGH | ✅ **FIXED** (`A0-2`): typed `ResearchEngineError`; zero-evidence reports abstain FAILED. |
| B6 cross-run evidence leakage | HIGH | ✅ **FIXED** (`A0-2`): `run_id` threaded through store; cross-run raises `EvidenceStoreError`. |
| B7 cross-workspace add silently dropped | MEDIUM | ✅ **FIXED** (`A0-2`): raises `EvidenceStoreError` instead of dropping. |
| B8 ReportClaim has no validator | MEDIUM | ✅ **FIXED** (`A0-2`): pydantic validator; SUPPORTED ≥1 evidence; citations resolve to chunks. |
| B9 V3 provenance dead defaults | MEDIUM | ✅ **FIXED** (`A0-2`): provenance populated explicitly; typed citations; findings derived from claims. |
| B10 tautological contract checks | MEDIUM | ✅ **FIXED** (`A0-2`): checks validate supplied arguments against real objects. |

**This submission:** B1 + B11 only. B2–B10 fixes follow as the next slice.

---

## 1. Current slice status

| Slice | Title | Status |
|---|---|---|
| **A0-1** | Core agent interfaces, lifecycle enums, schemas | ✅ COMMITTED — submitted to rew1 |
| A0-2 | Research run & evidence schemas | ⏳ Code written + tested locally; NOT yet committed/submitted |
| A1-1 | Enforced dispatcher + read-only tools | ⬜ Not started |
| A1-2 | Bounded calculator | ⬜ Not started |

---

## 2. A0-1 — Core agent interfaces (COMMITTED)

**Commit:** on `feat/v3-agents-research` (parent: `cca0f70`)

### Changed / created files
- `packages/agents/interfaces.py` — added `RunContext`, `RunState`, `RunStatus`, `StepEvent`, `StepStatus`, `Usage`, `RunBudget`, `ModelPolicy`, `RetryPolicy`, `ToolSpec`, `ToolClass`, `ToolPolicy`, `AgentErrorCode`, `RESERVED_CONTEXT_FIELDS`, `validate_tool_arguments`, `ContextOverrideError`, `InvalidRunTransitionError`. Existing prototype types (`AgentRun`, `AgentStep`, `AgentConfig`) preserved.
- `packages/agents/__init__.py` — re-exports for the new contracts.
- `packages/agents/contract_tests.py` — pure contract checks (no IO/services): `check_run_context_contract`, `check_tool_spec_contract`, `check_tool_policy_contract`, `check_run_state_contract`, `check_step_event_contract`, `check_tool_arguments_do_not_override_context`.
- `tests/unit/test_agents.py` — 15 new contract tests appended (fixture validation, frozen models, empty-tool policy, monotonic transitions, unknown-failure-blocks-completed, reserved-context rejection).
- `tests/fixtures/agents/run_context.example.json`, `tool_spec.example.json`, `step_event.example.json` — JSON examples that must validate against the typed contracts.

### Test commands and results
```bash
/root/ko-venv/bin/ruff check packages/agents/ tests/unit/test_agents.py   # clean
/root/ko-venv/bin/mypy packages/agents/interfaces.py packages/agents/contract_tests.py packages/agents/__init__.py  # clean
/root/ko-venv/bin/python -m pytest tests/unit/test_agents.py -q           # 21 passed
```

### Key contract decisions (A0-1)
- `RunContext` is frozen and server-created; tools cannot override reserved fields (`validate_tool_arguments` rejects them).
- `ToolPolicy` defaults to **deny**: empty `allowed_tools` means no tools.
- `RunState` transitions are monotonic; terminal states are one-way; `mark_unknown_required_failure()` makes `COMPLETED` unreachable.
- `StepEvent` stores action summaries + I/O *references*, never hidden reasoning; `sequence` strictly increases; failed/blocked steps must carry an `error_code`.
- `ToolSpec` requires `additionalProperties: false` on object schemas for strict dispatch.

---

## 3. A0-2 — Research schemas (written, NOT yet committed)

Code is complete and locally green; held for rew1 acceptance of A0-1 first.

- `packages/research/interfaces.py` — `EvidenceVerificationState`, extended `ResearchEvidence` (run/subquestion scoping, document version/chunk, quote span, source hash, verification state, revoke/delete stamps), `Citation`, `ClaimStatus`, `ReportClaim`, `ResearchRun`, extended `ResearchReport`.
- `packages/research/contract_tests.py` — `check_research_evidence_contract`, `check_report_claim_contract`, `check_evidence_run_isolation_contract`.
- `tests/unit/test_research_contracts.py` + `tests/fixtures/research/*.json`.
- Local result: 13 passed (research contracts + prototype research tests).

---

## 4. Submission status to rew1

- [ ] A0-1 submitted to rew1 (`w4:p1D`) — Herdr prompt with file list + test commands
- [ ] rew1 acceptance received
- [ ] A0-2 submitted to rew1
- [ ] A1-1 / A1-2 per lead's incremental directive

---

## 5. Blockers / dependencies

- **None blocking A0-1.** Full V3 still gated on: coordinator recording the LangGraph-in-v3 timing clarification (DECISIONS.md v4 row) before dependency install; v1 Q9 acceptance for shared-contract freeze.
- **Baseline note:** coordinator ledger cites "602 tests" on master; the exact command
  `pytest tests/contracts/ tests/unit/ tests/integration/ -q` on this branch yielded
  **397 passed, 5 skipped, 3 xfailed** (304 s) before my changes. Discrepancy noted
  for the coordinator; my slices are verified green in isolation.
- **Out of my scope (do not touch):** `packages/workflow_engine/**` (dev21 / Stream B V4 work-in-progress in the same working tree), `handoffs/COORDINATOR-HANDOFF.md`.
