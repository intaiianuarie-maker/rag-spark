# DEV21 — V4 Visual Workflows Handoff

**Agent**: dev21 (w4:p1B)
**Date**: 2026-10-12
**Milestone**: V4 — Visual Workflows
**Card**: W2 — Langflow Compatibility Spike

---

## Slice Status

| Slice | Description | Status |
|-------|-------------|--------|
| W2-1 R3 | Langflow parser interface & strict security rejection | ✅ **ACCEPTED by rew2** (2026-10-12) |
| W2-2 | Declarative node translation (TRIGGER, RETRIEVER, LLM, OUTPUT) | ✅ COMPLETE — submitted to rew2 |
| W2-3 | Edge/condition routing mapping (CONDITION, TRANSFORM, AGENT) + round-trip export | ⏳ BLOCKED — awaiting W2-2 acceptance |

---

## W2-1 R3: Parser Interface & Strict Security Rejection

### Review History

| Round | Date | Verdict |
|-------|------|---------|
| R1 | 2026-10-12 | ❌ REJECTED — 6 findings (F-1…F-6) |
| R2 | 2026-10-12 | ❌ REJECTED — 2 findings (R3-1, R3-2) |
| R3 | 2026-10-12 | ⏳ awaiting rew2 re-review |

### All Findings Addressed

**R1 findings (closed in R2, verified by rew2):**
- F-1: recursive `_walk` → iterative BFS, `MAX_DEPTH=100`
- F-2: near-miss key bypass → normalized boundary matching, edge scan
- F-3: duplicate node IDs silently dropped → `DUPLICATE_NODE_ID`
- F-4: empty flow accepted → `EMPTY_FLOW`
- F-5: `MAX_FIELD_CHARS` unenforced → enforced on display names + template values
- F-6: RUF022 lint → flat alphabetical `__all__`, full package green

**R2 findings (fixed in R3):**
- **R3-1 (CRITICAL, F-1 class):** `_template_value` (line 782) still recursed on
  `value`/`default` chains with no depth cap or cycle guard — 4 confirmed
  RecursionError escapes via the size-check path (`_check_template_sizes`).
  Fixed: rewritten iteratively with explicit stack, `MAX_TEMPLATE_DEPTH=32` cap,
  and cycle guard; exceed/cycle returns the `_DEPTH_EXCEEDED` sentinel which
  `_check_template_sizes` converts into a `BAD_FLOW_FORMAT` structured
  rejection (never a crash, never silent acceptance).
- **R3-2:** scan depth-exceed silently skipped then accepted. Fixed:
  `_find_code_markers` now returns `(markers, depth_exceeded)`; both call sites
  (node scan in `_resolve_component`, edge scan in `import_definition`) append
  an `OVERSIZED_FLOW` structured rejection when the scan was truncated.

### Changed / Created Files

| File | Action | Description |
|------|--------|-------------|
| `packages/workflow_engine/adapters/__init__.py` | Created | Adapter package init |
| `packages/workflow_engine/adapters/langflow_adapter.py` | Created | W2-1 implementation |
| `packages/workflow_engine/__init__.py` | Modified | Added W2 exports, flat alphabetical `__all__`, accurate docstring |
| `tests/unit/test_workflow_langflow_adapter.py` | Created | 68 tests (31 base + 25 R1 regression + 12 R2 regression) |
| `handoffs/DEV21-V4-HANDOFF.md` | Created | This document |

### Test Commands & Results (exact commands run)

```bash
# Full workflow suite (adapter + pre-existing W0/W1)
/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_validator.py tests/unit/test_workflow_compiler.py tests/unit/test_workflow_fake_runtime.py tests/unit/test_workflows.py -q
# 143 passed in 0.91s

# Adapter-only (W2-1 + W2-2)
/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py -q
# 95 passed in 0.62s

# Lint (full package scope)
/root/ko-venv/bin/ruff check packages/workflow_engine/
# All checks passed!

# Type check (full package scope)
/root/ko-venv/bin/mypy packages/workflow_engine/
# Success: no issues found in 8 source files
```

### Test Coverage (92 tests)

- **Valid flow passes security** (5 tests)
- **Custom code component rejection** (9 tests)
- **Unsupported component rejection** (2 tests)
- **Oversized flow rejection** (2 tests)
- **Malformed flow rejection** (4 tests)
- **No Langflow import** (2 tests)
- **Catalog** (4 tests)
- **Metadata** (3 tests)
- **R1 F-1 regression** (4 tests): dict/list depth 5000, self-reference, MAX_DEPTH
- **R1 F-2 regression** (13 tests): 11 near-miss keys, description false-positive, edge scan
- **R1 F-3 regression** (2 tests)
- **R1 F-4 regression** (2 tests)
- **R1 F-5 regression** (4 tests)
- **R2 R3-1 regression** (8 tests): value-chain 5000, default-chain 5000, self-ref value, self-ref default, mixed chain, shallow accepted, sentinel direct-call, MAX_TEMPLATE_DEPTH
- **R2 R3-2 regression** (4 tests)
- **W2-2 node translation** (27 tests): TRIGGER (4), RETRIEVER (4), LLM (5), OUTPUT (4), config extraction forms (4), validator integration (3)

Known coverage gaps (documented honestly): middle-embedded markers
(e.g. `mycodefield`) are accepted by design (rew2 advisory A-1, deferred to W2-2
design). The W2-2 config mapping reads only explicitly allowlisted template
fields and does not depend on the scan for safety — closed by design in this
slice. Edge/condition translation and export remain W2-3 (raise
`NotImplementedError`).

### Submission Status to rew2

- **R1**: submitted 2026-10-12, REJECTED (F-1…F-6)
- **R2**: submitted 2026-10-12, REJECTED (R3-1, R3-2)
- **R3**: submitted 2026-10-12, ✅ **ACCEPTED** — rew2 verified: 116 passed, ruff/mypy green, 31-probe battery 0 crashes / 23 structured rejections / 6 clean accepts. Acceptance report: `handoffs/REW2-V4-W2-1-R3.md`; ledger: `handoffs/REW2-REVIEW-HANDOFF.md`; coordinator notified via `handoffs/COORDINATOR-HANDOFF.md`. W2-2 released pending coordinator scheduling; A-1 advisory carried forward.

---

## W2-2: Declarative Node Translation (TRIGGER, RETRIEVER, LLM, OUTPUT)

### Design constraints (from rew2 advisory A-1)
Config mapping reads **only** explicitly allowlisted template fields
(`LangflowComponentSpec.config_fields`) — the security scan is never relied
upon for config-mapping safety.

### What W2-2 Delivers

`_build_node_spec` translates each allowlisted component into a full `NodeSpec`:
- **Ports** come from the component catalog (never from untrusted input) —
  typed `PortSpec` lists for inputs and outputs, with `required` flags.
- **Config** is populated only from the component's explicitly allowlisted
  template fields (`config_fields`); wrapper forms (`{"value": ...}` /
  `{"default": ...}`) and literal scalars are both extracted.  Non-allowlisted
  fields (e.g. `api_key`, `show_expander`) are **never** copied into config.

### Changed / Created Files (W2-2)

| File | Change |
|------|--------|
| `packages/workflow_engine/adapters/langflow_adapter.py` | Added `_build_node_spec`; wired full node translation into `import_definition`; removed leftover W2-1 stub; docstring updated to W2-2 scope |

### R1 review finding (fixed in R2)

**W2-2-R1 (F-5 class, MEDIUM):** flow-level `name` fallback was unbounded —
a 5 MB flow `"name"` imported silently into `WorkflowDefinition.name`.
Fixed: the flow fallback is length-checked against `MAX_FIELD_CHARS` →
`OVERSIZED_FIELD` structured rejection (reject, not truncate). Caller-supplied
`name=` is trusted API input and intentionally unguarded (asserted by test).

### W2-2 Test Coverage (27 tests)

- **TRIGGER translation** (4 tests): type, no input ports, `message` output port (string), no capabilities
- **RETRIEVER translation** (4 tests): type + `retriever` capability, query/documents port types, allowlisted config (top_k/collection), non-allowlisted fields ignored
- **LLM translation** (5 tests): type + `llm` capability, input ports with `system_message` optional, message output port, config mapping (model/temperature/max_tokens), `api_key` never enters config
- **OUTPUT translation** (4 tests): type, input ports with optional `message`, output port, no capabilities
- **Config extraction forms** (4 tests): `value` wrapper, `default` wrapper, list value, empty template → empty config
- **Validator integration** (3 tests): translated flow produces no `UNSUPPORTED_NODE_TYPE`/`UNSUPPORTED_CAPABILITY`/port-name errors; capability aggregation (single + mixed)
- **Flow name guard** (3 tests): oversized flow name rejected, normal accepted, caller `name=` precedence

### Submission Status to rew2

- **W2-2 R1**: submitted 2026-10-12, REJECTED (single F-5-class finding: unbounded flow-name fallback)
- **W2-2 R2**: resubmitted 2026-10-12 via `herdr agent prompt rew2` (pane w4:p1E) with flow-name guard + 3 tests

### Test Commands & Results (exact commands run)
