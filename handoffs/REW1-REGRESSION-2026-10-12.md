# REW1 — Stream A Regression Guard (2026-10-12)

**Reviewer:** rew1 (`w4:p1D`) · **Scope:** V3 Stream A (agents + research), read-only guard
**HEAD:** `dd9ce1115feb06ff7adf12c2e4d229238932f25a` (`main`) — unchanged since round-4 acceptance
**Reference:** `handoffs/REW1-REVIEW-HANDOFF.md` (ACCEPTED at §0c, follow-ups closed at §0d)

## Result: ✅ **PASS** — 47 green, no cross-stream regression

---

## 1. Gate outputs (all run on HEAD `dd9ce11`)

### 1.1 pytest — Stream A mandated files
```
/root/ko-venv/bin/pytest tests/unit/test_agents.py tests/unit/test_research.py tests/unit/test_research_contracts.py -v
```
```
collected 47 items

tests/unit/test_agents.py::test_calculator_tool PASSED                       [  2%]
tests/unit/test_agents.py::test_agent_runtime_react_multi_step PASSED        [  4%]
tests/unit/test_agents.py::test_agent_runtime_max_steps_exceeded PASSED      [  6%]
tests/unit/test_agents.py::test_run_context_fixture_validates PASSED         [  8%]
tests/unit/test_agents.py::test_run_context_rejects_missing_identity PASSED  [ 10%]
tests/unit/test_agents.py::test_run_context_rejects_naive_deadline PASSED    [ 12%]
tests/unit/test_agents.py::test_budget_bounds_are_validated PASSED           [ 14%]
tests/unit/test_agents.py::test_tool_spec_fixture_validates PASSED           [ 17%]
tests/unit/test_agents.py::test_tool_spec_requires_strict_schemas PASSED     [ 19%]
tests/unit/test_agents.py::test_tool_spec_rejects_empty_name_or_version PASSED [ 21%]
tests/unit/test_agents.py::test_empty_tool_policy_denies_everything PASSED   [ 23%]
tests/unit/test_agents.py::test_tool_policy_allows_only_listed PASSED        [ 25%]
tests/unit/test_agents.py::test_run_state_terminal_transitions_are_one_way PASSED [ 28%]
tests/unit/test_agents.py::test_run_state_completed_blocked_by_unknown_failure PASSED [ 30%]
tests/unit/test_agents.py::test_run_state_error_code_rejected_on_non_terminal PASSED [ 32%]
tests/unit/test_agents.py::test_step_event_fixture_validates PASSED          [ 34%]
tests/unit/test_agents.py::test_step_event_sequence_must_increase PASSED     [ 36%]
tests/unit/test_agents.py::test_step_event_failed_requires_error_code PASSED [ 38%]
tests/unit/test_agents.py::test_tool_arguments_cannot_override_context PASSED [ 40%]
tests/unit/test_agents.py::test_usage_cost_is_optional PASSED                [ 42%]
tests/unit/test_agents.py::test_enforced_dispatch_denies_unlisted_tool PASSED [ 44%]
tests/unit/test_agents.py::test_enforced_dispatch_rejects_reserved_context_override PASSED [ 46%]
tests/unit/test_agents.py::test_enforced_dispatch_validates_strict_schema PASSED [ 49%]
tests/unit/test_agents.py::test_enforced_dispatch_enforces_tool_call_budget PASSED [ 51%]
tests/unit/test_agents.py::test_enforced_dispatch_checks_deadline_before_spend PASSED [ 53%]
tests/unit/test_agents.py::test_enforced_dispatch_enforces_token_budget PASSED [ 55%]
tests/unit/test_agents.py::test_enforced_dispatch_enforces_output_size_bound PASSED [ 57%]
tests/unit/test_research.py::test_evidence_store_crud_and_isolation PASSED  [ 60%]
tests/unit/test_research.py::test_contradiction_detection PASSED            [ 62%]
tests/unit/test_research.py::test_research_engine_full_pipeline PASSED      [ 64%]
tests/unit/test_research.py::test_report_abstains_without_verified_evidence PASSED [ 66%]
tests/unit/test_research.py::test_planning_failure_raises_instead_of_faking PASSED [ 68%]
tests/unit/test_research.py::test_synthesis_provider_failure_raises PASSED  [ 70%]
tests/unit/test_research.py::test_evidence_run_scoping_blocks_cross_run_reads PASSED [ 72%]
tests/unit/test_research.py::test_cross_workspace_add_raises PASSED         [ 75%]
tests/unit/test_research.py::test_engine_uses_injected_model_policy PASSED  [ 77%]
tests/unit/test_research.py::test_revoked_evidence_excluded_from_report PASSED [ 79%]
tests/unit/test_research_contracts.py::test_evidence_fixture_validates PASSED [ 81%]
tests/unit/test_research_contracts.py::test_caller_supplied_claim_is_never_verified PASSED [ 83%]
tests/unit/test_research_contracts.py::test_evidence_quote_span_must_be_valid_range PASSED [ 85%]
tests/unit/test_research_contracts.py::test_report_claim_fixture_validates PASSED [ 87%]
tests/unit/test_research_contracts.py::test_supported_claim_requires_evidence_ids PASSED [ 89%]
tests/unit/test_research_contracts.py::test_unknown_claim_never_cites_fabricated_chunks PASSED [ 91%]
tests/unit/test_research_contracts.py::test_evidence_run_isolation PASSED  [ 93%]
tests/unit/test_research_contracts.py::test_research_run_scopes_investigation PASSED [ 95%]
tests/unit/test_research_contracts.py::test_report_abstains_without_verified_evidence PASSED [ 98%]
tests/unit/test_research_contracts.py::test_report_separates_disputed_candidates PASSED [100%]

============================== 47 passed in 0.93s ==============================
```
**47 passed — matches the accepted count exactly. No additions, no losses.**

### 1.2 ruff
```
/root/ko-venv/bin/ruff check packages/agents packages/research
→ All checks passed!
```

### 1.3 mypy
```
/root/ko-venv/bin/mypy packages/agents packages/research
→ Success: no issues found in 10 source files
```

---

## 2. Cross-stream regression check

The working tree is dirty with V1/V2 + Stream B edits, but **none of them touch Stream A**:

- `git status --short packages/agents packages/research tests/unit/test_agents.py tests/unit/test_research.py tests/unit/test_research_contracts.py` → **empty** (all Stream A files byte-identical to HEAD `dd9ce11`, the state I accepted in round 4)
- Dirty paths all belong to other streams: `packages/answer/*` + `packages/ingestion/*` + `apps/worker` + `scripts/` (V1/V2), `packages/workflow_engine/adapters/langflow_adapter.py` + `tests/unit/test_workflow_langflow.py` (Stream B), docs/handoffs (coordination)
- **Import boundary:** Stream A's only cross-package dependency is `models.interfaces` (`packages/models` — clean vs HEAD). Stream A does not import from any dirty package (`answer`, `ingestion`, `workflow_engine`, `retrieval`): zero coupling to the dirty surface

**Conclusion:** the dirty V1/V2 + Stream B edits cannot regress Stream A — no shared touched code, gates green at the accepted test count.

---

## 3. Notes

- This guard is read-only: no code edits, no commits, no verdict changes. Stream A's verdict remains ✅ ACCEPTED per `handoffs/REW1-REVIEW-HANDOFF.md`.
- This file is the only artifact created by this run.
