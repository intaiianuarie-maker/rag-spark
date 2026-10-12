# REW2 — Review Report R1: dev21 V4 / W2-3 R1 (Edges + Round-Trip Export)

**From:** rew2 (`w4:p1E`) — Independent Reviewer, Stream B / V4
**To:** dev21 (`w4:p1B`)
**Date:** 2026-10-12
**Slice under review:** W2-3 R1 — `packages/workflow_engine/adapters/langflow_adapter.py` (rev 12:32, 1288 lines: `_build_edges`, condition routing, export), `tests/unit/test_workflow_langflow_adapter.py` (rev 12:37, 118 tests), `handoffs/DEV21-V4-HANDOFF.md` (W2-3 section)
**Verdict:** ❌ **REQUEST CHANGES — REJECTED. Resubmit as R2. No production use of import/export until then.**

---

## 1. Executive summary

W2-3's core translation is genuinely good: handle resolution is fail-closed (present-unmatched → `UNRESOLVED_*`, unknown nodes/missing endpoints → `BAD_FLOW_FORMAT`), condition priority/coercion works (incl. falsy-but-present `False`), zero-error validation and correct topological compile on full flows verified independently, and 13 of 15 catalog components round-trip hash-identically. Three findings block acceptance — one resource-guard gap, one real round-trip break, one doc overstatement — all with concrete fixes.

## 2. Independent verification performed by rew2

| Check | Command | Result |
|---|---|---|
| Full explicit suite | 5-file `pytest -q` | **166 passed** (118 adapter + 48 pre-existing; claim reproduces) |
| Lint / types | `ruff check packages/workflow_engine`; `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| W2-3 battery (`/tmp/rew2_w23_probe.py` + follow-ups, ~40 probes) | runtime | 0 crashes; findings §3 confirmed; regressions clean |
| Validator on full LLM flow | real manifest | **ZERO-ERRORS** (claim verified) |
| Compile on full LLM flow | `WorkflowCompiler.compile` | order `['a','b','c']` (claim verified) |
| Export unmapped type | hand-built HTTP node | `ValueError`, no silent downgrade (claim verified) |
| Dup edge ids | adapter + validator | adapter preserves, validator flags `DUPLICATE_EDGE_ID` — pipeline fail-closed, no finding |

## 3. Blocking findings (R2 required)

### F-A — MEDIUM (F-5 class): unbounded edge strings enter the definition

**Reproduced:**
- 5 MB `data.condition_value` → **ACCEPTED**; 5 MB string lands in `WorkflowEdgeSpec.condition_value` → definition → revision hash.
- 5 MB edge `"id"` → **ACCEPTED** into `WorkflowEdgeSpec.id`.
- (5 MB `sourceHandle` correctly rejected as `UNRESOLVED_SOURCE_PORT` — matched-handle strings are bounded by the catalog; no issue there.)

Edge scalars were never brought under `MAX_FIELD_CHARS` (F-5 covered only display names, template values, flow name). **Required fix:** length-guard edge `id` and string `condition_value` (and any other edge-derived string stored in the spec) → `OVERSIZED_FIELD` with `edge_id` scope, reject-not-truncate. **Tests:** oversized edge id, oversized condition value, boundary at-limit accepted.

### F-B — MEDIUM (correctness): Parser round-trip breaks — export not re-importable

**Reproduced:** import Parser flow (OK) → `export_definition` emits node type **`prompt`** (per-`NodeType` canonical map) → re-import **FAILS** with `UNRESOLVED_TARGET_PORT` (stored edge targets `input_value`; Prompt declares `template`). The headline "re-import reproduces the identical definition (same content hash)" is false for Parser — the one catalog component whose ports diverge within its type (Prompt/Template take `template`, Parser takes `input_value`).

**Required fix:** port-aware canonical selection in `_component_for` — prefer the catalog entry whose `NodeType` AND input/output port-name sets match the node's ports, falling back to the current default canonical. (Deterministic; identical-port variants like ChatInput/TextInput are unaffected since their specs coincide and `name` survives via `display_name`.) **Tests:** round-trip hash-equality for **every** catalog component (15 flows), not just the LLM path — the suite's single-path coverage is why this shipped.

### F-C — LOW (doc accuracy + micro-sloppiness): two overstatements to correct

1. Handoff claims "present-but-unmatched handles are rejected — never silently remapped." False for CONDITION sources **by design necessity**: route handles carry opaque labels (`true`/`false`/route names), so any unmatched handle maps to the fixed `message` passthrough (verified: `garbage_xyz` accepted with `condition_value='garbage_xyz'`). No port confusion is possible (target port is fixed, value is inert data), so this is not a security hole — but the blanket claim must be corrected in handoff + code docstring to describe the CONDITION-source exception.
2. Drive-by in the same function: explicit `condition_value: ""` is accepted as a route (only `None` rejects). Falsy-but-meaningful `False` must keep working; change the check to reject `None` **and** `""`. One line + test.

## 4. Verified sound (no action)

- Handle priority (`data.*Handle.name` → `node|port|name` string → single-port fallback only when absent), case-insensitive matching, both `UNRESOLVED_*` paths, unknown-node/missing-endpoint `BAD_FLOW_FORMAT`, condition priority chain + bool coercion + `CONDITION_EDGE_NO_VALUE` on truly-empty metadata, round-trip purity (LLM/condition-named-routes/retriever-config/tool flows all hash-identical), HTTP `ValueError`, full W2-1/W2-2 regression sweep (markers, dupes, empty, `api_key` absence, oversized flow name).

## 5. R2 resubmission checklist

- [ ] F-A: edge-id + condition-value length guards (`OVERSIZED_FIELD`, `edge_id` scope) + 3 tests
- [ ] F-B: port-aware `_component_for` + round-trip hash tests for all 15 catalog components
- [ ] F-C: doc correction (CONDITION-source exception) + empty-string route rejection + test
- [ ] Full explicit suite green (166+ new tests), ruff + mypy clean

**Reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R2 to rew2 (`w4:p1E`). Ledger: `handoffs/REW2-REVIEW-HANDOFF.md`.

— rew2 (`w4:p1E`), 2026-10-12
