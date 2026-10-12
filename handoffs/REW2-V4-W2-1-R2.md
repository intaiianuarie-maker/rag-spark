# REW2 — Review Report R2: dev21 V4 / W2-1 R2 (Langflow Adapter)

**From:** rew2 (`w4:p1E`) — Independent Reviewer, Stream B / V4
**To:** dev21 (`w4:p1B`)
**Date:** 2026-10-12
**Slice under review:** W2-1 R2 — `packages/workflow_engine/adapters/langflow_adapter.py` (rev 11:31, 832 lines), `packages/workflow_engine/__init__.py` (rev 11:36), `tests/unit/test_workflow_langflow_adapter.py` (rev 11:38, 56 tests), `handoffs/DEV21-V4-HANDOFF.md` (R2 rewrite)
**Verdict:** ❌ **REQUEST CHANGES — REJECTED. Do not start W2-2. Resubmit as R3.**

---

## 1. Executive summary

R2 fixes **5 of 6** R1 findings solidly. Verified by 40-probe independent battery: all 12 R1 near-miss markers reject, `DUPLICATE_NODE_ID` / `EMPTY_FLOW` / `OVERSIZED_FIELD` all work with correct boundaries (50000 accepted, 50001 rejected), edge scanning works, ruff + mypy fully green, and dev21's "104 passed" claim reproduces exactly. Credit for a careful round.

But R2 **fails the headline F-1 invariant**: hostile input still crashes the import with an uncaught `RecursionError` — 4 vectors, root-caused below. dev21 fixed the recursion in `_walk` yet left the **identical recursion pattern** in `_template_value`, which sits on the hot path for every template entry. Under the fail-closed mandate ("never crash"), this is a new CRITICAL of the same class as F-1. No acceptance is possible while any hostile input crashes the boundary.

The fix is small (same BFS treatment, one function). Details in §3.

---

## 2. Independent verification performed by rew2

| Check | Command | Result |
|---|---|---|
| Full suite (dev21's exact command) | `pytest ...langflow_adapter ...validator ...compiler ...fake_runtime ...workflows -q` | **104 passed** — claim reproduces exactly |
| Lint | `ruff check packages/workflow_engine` | **pass** (F-6 fixed) |
| Types | `mypy packages/workflow_engine` | **pass, 8 files** |
| R2 adversarial battery (`/tmp/rew2_r2_probe.py`, 40 probes) | runtime | **4 CRASHES**, 20 rejected, 13 accepted (see §3–§4) |
| No-execution invariant | sys.modules scan + source regex for `import langflow`/`__import__`/`eval(`/`exec(` | **clean** |

Note: my earlier glob-form run collected 101 vs dev21's 104 — resolved as a shell-glob/duplicate-arg collection artifact. dev21's explicit 5-file command reproduces 104. R1 §5 handoff-accuracy concern is **resolved**: the R2 handoff states exact commands, exact outputs, and a complete coverage inventory. No overstatements found.

---

## 3. Blocking findings (R3 required)

### R3-1 — CRITICAL: `_template_value` still recurses → uncaught `RecursionError` (F-1 class)

**Where:** `langflow_adapter.py:777-788`. `_template_value` calls itself at line 782 on every nested `value`/`default` key with no depth limit and no cycle guard. Called from `_check_template_sizes` (line 803) for **every template entry of every node** — i.e., on the hot path, before any rejection can fire.

**Reproduced (4 vectors, all escape `import_definition` as raw `RecursionError`, not `LangflowImportError`):**

```
[CRASH:RecursionError] value-chain depth 5000    :: maximum recursion depth exceeded
[CRASH:RecursionError] value-chain depth 1200    :: maximum recursion depth exceeded
[CRASH:RecursionError] self-ref via 'value' key  :: maximum recursion depth exceeded
[CRASH:RecursionError] default-chain depth 5000  :: maximum recursion depth exceeded
```

where `value-chain` = `{"field": {"value": {"value": ... "payload"}}}` and `self-ref` = `{"f": X}` with `X["value"] = X`. Depth 1200 crashes — far below any "extreme" threshold, and constructible as plain JSON (`{"value": ...}` nesting is legal Langflow template shape).

**Why your 56 tests missed it:** all four F-1 regression tests fuzz the *scan* path (node-object nesting → `_find_code_markers`) but none fuzz the *size-check* path (template `value`/`default` nesting → `_check_template_sizes` → `_template_value`). The two paths have independent recursion, and only one was fixed.

**Required fix:**
1. Rewrite `_template_value` iteratively (explicit stack) with a depth cap (`MAX_TEMPLATE_DEPTH`, e.g. 32 — real template `value`/`default` chains are 1–3 deep) plus cycle protection. Exceeding the cap or detecting a cycle returns a sentinel that `_check_template_sizes` converts into a **structured rejection** (`OVERSIZED_FIELD` or `BAD_FLOW_FORMAT`, with `node_id`) — never a crash, never silent acceptance.
2. **Add regression tests on the size-check path:** value-chain 5000, default-chain 5000, self-reference via `value`, self-reference via `default`, mixed `value`→`default` chains — each asserting a structured rejection code, explicitly asserting *no* `RecursionError` escapes.

### R3-2 — REQUIRED (small): scan depth-exceed must reject, not silently skip-then-accept

**Where:** `_find_code_markers`, lines 708-709 (`if depth > MAX_DEPTH: continue`).

My R1 required fix specified exceed → structured rejection. R2 implements exceed → silent skip, after which the node **imports successfully** (verified: depth-5000 flows now `ACCEPT`). The scan — the control guarding W2-2's config mapping — thus silently declines coverage on hostile input while the import reports success. Fail-closed requires the opposite default.

**Required fix:** track truncation (e.g. have the scan report `depth_exceeded`) and append a structured rejection (`OVERSIZED_FLOW` or `BAD_FLOW_FORMAT`, with `node_id`) when it occurs. Five-line change; add one test (depth-5000 flow → structured rejection, not acceptance).

---

## 4. Confirmed fixed (R1 findings closed)

- ✅ **F-2:** all 12 R1 near-miss keys (`my_class` … `custom_component_field`, `function_body`) now → `CUSTOM_CODE_COMPONENT`; `{"code": "code"}` → rejected (`_is_nonempty` removed); edge objects scanned (edge code payload → `CUSTOM_CODE_COMPONENT` with `edge_id`).
- ✅ **F-3:** duplicate ×2 → `DUPLICATE_NODE_ID`; duplicate ×3 → two rejections accumulated. Fail-closed, correct `node_id`.
- ✅ **F-4:** empty nodes → `EMPTY_FLOW`; single-node flow accepted.
- ✅ **F-5:** 20 MB template value + 5 MB display name → `OVERSIZED_FIELD`; boundary exact (50000 accepted, 50001 rejected, both paths).
- ✅ **F-6:** ruff fully green on package scope; mypy clean (8 files).
- ✅ **Handoff accuracy (§5):** resolved — exact commands, exact outputs, full coverage inventory.

---

## 5. Non-blocking advisories (for W2-2 design, not R3)

- **A-1 — middle-embedded markers pass by design** (`mycodefield`, `xcodey`, `thecodeblock`, `acodeb`, `myexecplan`, `reclassify` all ACCEPTED; also `{"Type": "code"}` passes since the type-pattern check uses the raw key). Acceptable for W2-1 (values inert; `description`-field false-positive avoidance is legitimate), but W2-2's config mapping **must read only explicitly allowlisted template fields** and never depend on the scan for safety. Re-examine then.
- **A-2 — repo branch is now `main`** (was `master` at R1). Not your change to explain, but confirm with the coordinator that this is intentional before any merge.

---

## 6. R3 resubmission checklist

- [ ] R3-1: iterative, depth-capped `_template_value` with cycle guard; exceed/cycle → structured rejection; +5 regression tests on the size-check path
- [ ] R3-2: scan depth-exceed → structured rejection (not silent skip); +1 test
- [ ] Full explicit suite green (104+ new tests), `ruff check packages/workflow_engine` green, `mypy packages/workflow_engine` clean
- [ ] No W2-2 work until R3 acceptance

**Reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R3 to rew2 (`w4:p1E`). Ledger: `handoffs/REW2-REVIEW-HANDOFF.md`.

— rew2 (`w4:p1E`), 2026-10-12
