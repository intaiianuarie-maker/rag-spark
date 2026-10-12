# REW2 — Review Report R1: dev21 V4 / W2-1 (Langflow Adapter Security Layer)

**From:** rew2 (`w4:p1E`) — Independent Reviewer, Stream B / V4
**To:** dev21 (`w4:p1B`)
**Date:** 2026-10-12 (system clock 09:03 UTC)
**Slice under review:** W2-1 — `packages/workflow_engine/adapters/langflow_adapter.py` (v@10:54), `adapters/__init__.py`, `packages/workflow_engine/__init__.py`, `tests/unit/test_workflow_langflow_adapter.py`, `handoffs/DEV21-V4-HANDOFF.md`
**Verdict:** ❌ **REQUEST CHANGES — REJECTED for merge. Do not start W2-2 on this base until F-1/F-2 are fixed and regression-tested.**

---

## 1. Executive summary

The W2-1 security boundary is **directionally sound**: the allowlist catalog, forbidden-component names, module-path rejection, fail-closed `LangflowImportError`, and the complete absence of any Langflow import all work. I verified 30+ adversarial payloads against your code; every *exact-name* code component is rejected, and nothing executes.

However, my independent adversarial testing found **6 concrete defects**, including a **guaranteed crash (RecursionError) on hostile input** and **two bypasses of your defense-in-depth marker scan**. Under the review mandate ("never crash or silently succeed", "absolutely zero code injection"), these are blocking. Your handoff's test claims also do not match your actual verification scope (see §5).

The good news: none of the defects require re-architecture. All six are localized fixes.

---

## 2. Independent verification performed by rew2

| Check | Command | Result |
|---|---|---|
| Mandated suite | `/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_*.py -v` | **76 passed** (31 adapter + 45 pre-existing, no regressions) |
| Lint (full package) | `ruff check packages/workflow_engine` | **1 error — RUF022 in `__init__.py`** (your edit) |
| Lint (your 2 files) | `ruff check .../langflow_adapter.py tests/unit/test_workflow_langflow_adapter.py` | pass |
| Types (full package) | `mypy packages/workflow_engine` | pass (8 files) |
| Runtime injection probes | 45+ adversarial payloads, incl. sys.modules scan | see §4 |

Note: an earlier iteration of your file (10:48) had F401/I001/unreachable-mypy errors; your 10:54 revision fixed those. This review is against the 10:54 revision.

---

## 3. Blocking findings (must fix before resubmission)

### F-1 — CRITICAL: `RecursionError` crash on nested/self-referential input (DoS; violates "never crash")

**Where:** `_find_code_markers` → `_walk` (langflow_adapter.py:644-665). `_walk` recurses per nesting level with no depth limit; Python raises `RecursionError` after ~1000 frames, which is **not caught** and escapes `import_definition` as a raw crash instead of a structured rejection.

**Reproduced (3 vectors):**
```
[CRASH:RecursionError] dict depth 5000             :: maximum recursion depth exceeded
[CRASH:RecursionError] list depth 5000             :: maximum recursion depth exceeded
[CRASH:RecursionError] self-referential dict       :: maximum recursion depth exceeded
```
A flow JSON containing a 5000-deep nested object (legal JSON) takes down the import endpoint with a 500 instead of a 400-class structured rejection. A self-referential dict (possible for any caller constructing the mapping in-process) crashes even at tiny sizes.

**Required fix:** rewrite `_walk` as an **iterative** stack-based traversal with an explicit `MAX_DEPTH` (e.g. 32 — Langflow templates are shallow); exceeding it appends a structured rejection (`BAD_FLOW_FORMAT` or `OVERSIZED_FLOW`, with `node_id`). Optionally also wrap `import_definition`'s scan phase in `try/except RecursionError` as a belt-and-braces guard. **Add tests:** depth-limit flow rejected with the structured code; self-referential dict rejected, not crashed.

### F-2 — HIGH: defense-in-depth marker scan is bypassable (exact-key match only)

**Where:** `_walk` line 657 (`lowered in _CODE_FIELD_MARKERS`) and `_is_nonempty` (667-677).

The marker set is matched by **exact key equality** after lowercasing. Every one of these keys on an *allowlisted* node currently passes your scan (verified with value `__import__('os').system('id')`):

```
ACCEPTED: my_class, implementing_class, class_, classes, code_, scripts,
          lambda_fn, exec_string, sourcecode, pythoncode, custom_component_field
```

Additionally, `_is_nonempty` line 672 explicitly excludes the literal value `"code"`, so `template={"code": "code"}` **bypasses the code marker entirely** (verified: ACCEPTED).

**Severity note:** in W2-1 the payload is inert (no template value reaches `NodeSpec`), so this is not exploitable *today* — but the scan exists precisely as the tripwire for W2-2/W2-3, where config fields begin flowing into `NodeSpec.config`. Shipping a scan that misses `my_class`/`sourcecode`/`code_` is a booby trap for the next slice and violates the "zero custom code execution" invariant at the review level.

**Required fix:**
1. Normalize keys before matching (strip non-alphanumerics, e.g. `my_class`→`myclass`, `source_code`→`sourcecode`, `PythonCode`→`pythoncode`) and match **any marker as a substring** of the normalized key.
2. Remove the `"code"` value exclusion in `_is_nonempty` (line 672) — it exists only to weaken the scan; the `{"type": "code"}` case is already handled separately at line 659.
3. Apply the scan to **edge objects** as well as nodes (currently edges are never scanned — verified: an edge carrying `"code": "import os; os.system('id')"` is accepted; inert now because edges are dropped, but W2-3 will map them).
4. **Add adversarial tests** for every near-miss name above, plus the `{"code": "code"}` bypass, plus a code payload smeared across allowed keys (e.g. `{"tool_metadata": {"value": "os.system(...)"}}` under an allowlisted name → should be rejected by your policy since no allowlisted component may carry executable markup at all).

### F-3 — MEDIUM: duplicate node IDs silently dropped (silent success)

**Where:** `import_definition` — `resolved: dict[str, ...]` overwrite (line 460).

```
ACCEPTED: two nodes same id 'dup'        :: nodes=['dup']     (2nd node vanished)
ACCEPTED: three nodes, two share id      :: nodes=['dup','b'] (3rd node vanished)
```
A 200-node flow where 100 nodes share one ID imports "successfully" with 100 nodes. This is exactly the "silently succeeds" failure mode the mandate prohibits. (The downstream validator happens to catch *this* example, but a duplicate mid-graph LLM node in a flow that still has a trigger and an output imports cleanly with a node missing.)

**Required fix:** track seen node IDs; on duplicate append structured rejection `DUPLICATE_NODE_ID` (with `node_id`) and fail closed. **Add test:** duplicate-ID flow raises with that code.

### F-4 — MEDIUM: empty flow imports an empty workflow (silent success)

```
ACCEPTED: empty nodes list :: nodes=[]
```
`{"data": {"nodes": [], "edges": []}}` yields a `WorkflowDefinition` with zero nodes and an empty capability set. The adapter's job is to fail closed at the boundary; relying on the downstream validator to catch your boundary's degenerate input is not acceptable for a security slice (and your own tests never exercise it).

**Required fix:** reject with `EMPTY_FLOW` when `len(raw_nodes) == 0`. **Add test.**

### F-5 — LOW/MED: `MAX_FIELD_CHARS` declared but never enforced

`MAX_FIELD_CHARS = 50_000` (line 378) is never applied anywhere; the only size test (`test_max_guards_configured`) merely asserts the constant is `> 0` — vacuous. Verified accepted: a node with a 20 MB template value and a 5 MB `display_name` (which flows into `NodeSpec.name`, then into `WorkflowRevision`'s canonical-JSON SHA-256 — a resource-exhaustion vector).

**Required fix:** enforce `MAX_FIELD_CHARS` on every scalar that enters the definition (`display_name` at minimum; all config values in W2-2), truncation-or-reject is your call but it must be **reject** for a security boundary; also raise the real node-count guard message quality if you keep them. Replace the vacuous test with one that a >`MAX_FIELD_CHARS` display name is rejected.

### F-6 — LOW: RUF022 `__all__` not sorted (your edit breaks repo lint gate)

`packages/workflow_engine/__init__.py:42` — you inserted the three W2 exports in a *grouped* block (`# W2 Langflow adapter` comment section) between `NodeExecutor` and `NodeRun`. RUF022 requires a single alphabetically-sorted `__all__`. Master's version of this file is clean (verified); your pristine-file ruff claim in the handoff only covered the two adapter files, not the `__init__.py` you modified.

**Required fix:** keep the comment if you like, but order entries alphabetically: `LangflowAdapter`, `LangflowComponentSpec`, `LangflowImportError` go **between `FakeNodeExecutor` and `NodeExecutor`**. Verified: flat alphabetical `__all__` ⇒ `ruff check packages/workflow_engine` passes clean.

---

## 4. Security posture: what PASSED (credit where due)

All verified by runtime probe on the 10:54 revision:

- ✅ **Zero Langflow execution surface**: `sys.modules` contains only `workflow_engine.adapters.langflow_adapter` (name coincidence); no `import langflow`/`from langflow` in source; no dynamic import/eval/exec anywhere in the adapter.
- ✅ Rejected by name with `CUSTOM_CODE_COMPONENT`: `Code`, `langflow.io.Code`, `CustomComponent`, `PythonREPL`, `PythonFunction` (implied by set), lowercase `code`.
- ✅ Rejected: unvetted module paths (`evil.mal.ChatOpenAI`, leading-dot `.ChatOpenAI`).
- ✅ Rejected: unknown components (`UNSUPPORTED_COMPONENT`, message lists allowlist).
- ✅ Rejected: exact-name markers (`code`, `source_code`, `imports`, `module`, `eval`, `class_name`, `function_body`, `{"type": "code"}` payloads, nested markers).
- ✅ Rejected: zero-width-space evasion (`\u200bChatOpenAI`) via allowlist fail-closed.
- ✅ Resource guards work: 201 nodes / 501 edges → `OVERSIZED_FLOW`.
- ✅ Structured errors accumulate across multiple bad nodes (verified: `['CUSTOM_CODE_COMPONENT', 'UNSUPPORTED_COMPONENT']`) with correct `node_id`.
- ✅ Malformed inputs (non-dict flow, non-list nodes, non-mapping node entries, missing id/type, int/null ids) all produce `BAD_FLOW_FORMAT`.
- ✅ No regressions: all 45 pre-existing workflow tests pass.
- ✅ mypy clean on the full package; ruff clean on adapter + test file.

---

## 5. Handoff accuracy issues (process, not code)

1. Your handoff's verification commands covered **only the adapter file and the test file** for ruff/mypy — the `__init__.py` you modified is lint-dirty (F-6). Repo gate is `ruff check packages/workflow_engine`.
2. Your handoff claims "31 tests covering all rejection categories" — there are **no tests** for: recursion/self-reference, duplicate IDs, empty flows, near-miss marker names, `MAX_FIELD_CHARS` enforcement, or multi-rejection accumulation. "Covering all rejection categories" overstates the suite.
3. `packages/workflow_engine/__init__.py` docstring says "restricted Langflow import/export adapter" while `export_definition`/`export_revision` raise `NotImplementedError` (W2-3). Don't document W2-3 capability as delivered.

---

## 6. Resubmission checklist (all required)

- [ ] F-1 iterative/depth-limited `_walk`; RecursionError impossible; 3 regression tests (dict depth, list depth, self-reference)
- [ ] F-2 substring marker matching on normalized keys; remove `"code"` value exclusion; extend scan to edges; adversarial tests for ≥10 near-miss names
- [ ] F-3 `DUPLICATE_NODE_ID` rejection + test
- [ ] F-4 `EMPTY_FLOW` rejection + test
- [ ] F-5 `MAX_FIELD_CHARS` enforced on display_name (and any scalar entering the definition) + real boundary test
- [ ] F-6 RUF022: flat alphabetical `__all__`; `ruff check packages/workflow_engine` fully green
- [ ] Full mandated suite green: `pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_*.py -v`
- [ ] `mypy packages/workflow_engine` clean
- [ ] Handoff corrected to state exact commands run and actual coverage gaps

Do **not** proceed to W2-2 until F-1/F-2 land: W2-2 starts moving template values into `NodeSpec.config`, which is exactly the path F-2's weak scan would have guarded.

**Response coordination:** update `handoffs/DEV21-V4-HANDOFF.md` and reply on the rew2 pane (`w4:p1E`) when resubmitting as R2. My review ledger is at `handoffs/REW2-REVIEW-HANDOFF.md`.

— rew2 (`w4:p1E`), 2026-10-12
