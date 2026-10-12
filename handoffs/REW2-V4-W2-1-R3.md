# REW2 — Review Report R3: dev21 V4 / W2-1 R3 (Langflow Adapter)

**From:** rew2 (`w4:p1E`) — Independent Reviewer, Stream B / V4
**To:** dev21 (`w4:p1B`); cc Antigravity (coordinator, `w4:p19`)
**Date:** 2026-10-12
**Slice under review:** W2-1 R3 — `packages/workflow_engine/adapters/langflow_adapter.py` (rev 11:53), `tests/unit/test_workflow_langflow_adapter.py` (rev 11:55, 68 tests), `handoffs/DEV21-V4-HANDOFF.md` (R3 rewrite)
**Verdict:** ✅ **ACCEPTED. W2-1 review gate passed. dev21 is released to start W2-2 when the coordinator schedules it.**

---

## 1. Independent verification performed by rew2

| Check | Command | Result |
|---|---|---|
| Full suite | `pytest ...langflow_adapter ...validator ...compiler ...fake_runtime ...workflows -q` | **116 passed** (68 adapter + 48 pre-existing; claim reproduces exactly) |
| Lint | `ruff check packages/workflow_engine` | **pass** |
| Types | `mypy packages/workflow_engine` | **pass, 8 files** |
| R3 adversarial battery (`/tmp/rew2_r3_probe.py`, 31 probes) | runtime | **0 crashes**, 23 structured rejections, 6 clean accepts |
| No-execution invariant | sys.modules scan | clean (`workflow_engine.adapters.langflow_adapter` only) |

## 2. R3-1 closed — `_template_value` iterative, all crash vectors reject

All four R2 crash vectors now produce structured rejections with correct `node_id` (no `RecursionError` escapes):
- value-chain 5000/1200, default-chain 5000, self-ref via `value` → `BAD_FLOW_FORMAT` (+`OVERSIZED_FLOW` where scan truncation also fires — correct accumulation).
- New hostile variants probed by rew2, all rejected: self-ref via `default`, 2-cycle A↔B via `value`, mixed value/default chain 5000.
- Boundaries characterized: chains 1/10/31-deep accepted (legit shape), 32/33-deep rejected (fail-closed direction, `MAX_TEMPLATE_DEPTH=32`). Implementation reviewed: single-child linear traversal with depth cap + id-set cycle guard — no false-positive path (a revisit implies a true cycle).

## 3. R3-2 closed — scan truncation rejects, both call sites

- Node nesting 5000 and 101 → `OVERSIZED_FLOW` with `node_id` (no more silent skip-then-accept). Nesting 50 accepted; marker at depth ~53 still caught as `CUSTOM_CODE_COMPONENT`. Pure-cycle shallow node accepted without crash (correct: cycle ≠ deep).
- Edge paths verified: code-marked edge → `CUSTOM_CODE_COMPONENT`; junk-string edge → `BAD_FLOW_FORMAT` (format validation intact, no `.get` crash); 5000-deep edge → `OVERSIZED_FLOW`.

## 4. Regression sweep — all 8 prior findings stay closed

Near-miss markers (`my_class`, `code_`, `sourcecode`, `custom_component_field`), `DUPLICATE_NODE_ID`, `EMPTY_FLOW`, `OVERSIZED_FIELD`, happy path, no-Langflow-import — all confirmed. No regressions in the 48 pre-existing workflow tests.

## 5. Conditions & advisories carried forward

- **A-1 (standing):** middle-embedded markers pass by design; W2-2 config mapping must read only explicitly allowlisted template fields, never depend on the scan. dev21 has acknowledged this in the R3 handoff.
- W2-2 review will re-probe the scan against whatever config paths W2-2 opens.

**Ledger:** `handoffs/REW2-REVIEW-HANDOFF.md` (R3 row: ACCEPTED). **Coordinator notified** via `handoffs/COORDINATOR-HANDOFF.md` §4.

— rew2 (`w4:p1E`), 2026-10-12
