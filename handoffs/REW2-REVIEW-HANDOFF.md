# REW2 — Review Ledger (Stream B / V4)

**Reviewer:** rew2 (`w4:p1E`) — independent testing, typing, and code-injection security rejection gate
**Stream:** B / V4 (Visual Workflows) — Developer: dev21 (`w4:p1B`), Card W2 (Langflow adapter)
**Repo:** `/root/code/Super Rag/knowledge-os` (branch `master`) · **Ledger created:** 2026-10-12

---

## Submission Log

| # | Slice | Received | Reviewed | Verdict | Report |
|---|-------|----------|----------|---------|--------|
| R1 | W2-1 — Langflow adapter: parser interface & strict security rejection | 2026-10-12 (dev21 submission via `herdr agent prompt rew2`; adapter final rev 10:54, tests 10:55, handoff 10:57) | 2026-10-12 (09:03 UTC system clock) | ❌ **REJECTED — REQUEST CHANGES** | `REW2-V4-W2-1-R1.md` |
| R2 | W2-1 R2 — all 6 R1 findings addressed (adapter rev 11:31, 832 lines; tests rev 11:38, 56 tests; handoff R2 rewrite) | 2026-10-12 (dev21 resubmission; branch now `main`, was `master` at R1) | 2026-10-12 | ❌ **REJECTED — REQUEST CHANGES** | `REW2-V4-W2-1-R2.md` |
| R3 | W2-1 R3 — R3-1 (`_template_value` iterative + sentinel) & R3-2 (scan truncation rejects) fixed; 68 adapter tests; handoff R3 rewrite | 2026-10-12 (dev21 resubmission) | 2026-10-12 | ✅ **ACCEPTED** | `REW2-V4-W2-1-R3.md` |
| W2-2 R1 | W2-2 — declarative node translation (typed ports + allowlisted config); 92 adapter tests; handoff W2-2 section | 2026-10-12 (dev21 submission; branch `main`) | 2026-10-12 | ❌ **REJECTED — REQUEST CHANGES (single fix)** | `REW2-V4-W2-2-R1.md` |
| W2-2 R2 | W2-2 R2 — flow-name `OVERSIZED_FIELD` guard + 3 tests (95 adapter tests) | 2026-10-12 (dev21 resubmission) | 2026-10-12 | ✅ **ACCEPTED** | `REW2-V4-W2-2-R2.md` |
| W2-3 R1 | W2-3 — edges + condition routing + round-trip export (1288-line adapter; 118 adapter tests) | 2026-10-12 (dev21 submission; branch `main`) | 2026-10-12 | ❌ **REJECTED — REQUEST CHANGES** | `REW2-V4-W2-3-R1.md` |
| W2-3 R2 | W2-3 R2 — edge guards + port-aware export + doc fix (145 adapter tests) | 2026-10-12 (dev21 resubmission) | 2026-10-12 | ❌ **REJECTED — REQUEST CHANGES (F-5 closure)** | `REW2-V4-W2-3-R2.md` |
| W2-3 R3 | W2-3 R3 — node-id guard + `_trunc` echo sweep (155 adapter tests) | 2026-10-12 (dev21 resubmission) | 2026-10-12 | ❌ **REJECTED — REQUEST CHANGES (single line)** | `REW2-V4-W2-3-R3.md` |
| W2-3 R4 | W2-3 R4 — marker-set truncation at insertion (158 adapter tests) | 2026-10-12 (dev21 resubmission) | 2026-10-12 | ✅ **ACCEPTED — W2 card complete** | `REW2-V4-W2-3-R4.md` |

### R1 scope audited
- `packages/workflow_engine/adapters/langflow_adapter.py` (722 lines, rev 10:54)
- `packages/workflow_engine/adapters/__init__.py`
- `packages/workflow_engine/__init__.py` (modified: W2 exports added)
- `tests/unit/test_workflow_langflow_adapter.py` (31 tests)
- `handoffs/DEV21-V4-HANDOFF.md`
- Note: working tree also contains dev11's Stream A (V3) uncommitted changes (`packages/agents/*`, `packages/research/*`, `tests/unit/test_agents.py`, `tests/unit/test_research_contracts.py`) — **out of my review scope**; flagged to coordinator for scoped merge handling.

---

## R1 — Independent test commands & outputs

| Command | Output |
|---|---|
| `/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_*.py -v` | **76 passed** (31 adapter + 45 pre-existing workflow tests; no regressions) |
| `ruff check packages/workflow_engine` | **1 error: `__init__.py:42:11 RUF022 __all__ is not sorted`** (dev21's edit; master version is clean) |
| `ruff check packages/workflow_engine/adapters/langflow_adapter.py tests/unit/test_workflow_langflow_adapter.py` | all checks passed |
| `mypy packages/workflow_engine` | Success: no issues found in 8 source files |
| Adversarial runtime probes (45+ payloads, rew2-authored, deleted after run) | see below |

---

## R1 — Security check results

**Zero custom code execution: PASSED.** Adapter never imports/installs/executes Langflow; `sys.modules` scan clean (only `workflow_engine.adapters.langflow_adapter`); no eval/exec/dynamic import in source. Confirmed **rejected** with structured `LangflowImportError`: `Code`, `langflow.io.Code`, `CustomComponent`, `PythonREPL`, lowercase `code`, unvetted module paths (`evil.mal.ChatOpenAI`, `.ChatOpenAI`), unknown components (`UNSUPPORTED_COMPONENT`), exact-name markers (`code`, `source_code`, `imports`, `module`, `eval`, `class_name`, `function_body`, `{"type":"code"}` payloads), oversized flows (201 nodes / 501 edges → `OVERSIZED_FLOW`), all malformed-format probes → `BAD_FLOW_FORMAT`; multi-rejection accumulation works with correct `node_id`.

**Defects found (evidence by runtime probe):**
1. **CRITICAL — RecursionError crash (uncaught, non-structured):** `_walk` (langflow_adapter.py:644-665) recurses without depth limit. `dict depth 5000`, `list depth 5000`, and self-referential dict all crash `import_definition` instead of rejecting. Violates "never crash" invariant.
2. **HIGH — marker scan bypass:** exact-key matching only. Verified ACCEPTED on allowlisted node with payload `__import__('os').system('id')` under keys: `my_class`, `implementing_class`, `class_`, `classes`, `code_`, `scripts`, `lambda_fn`, `exec_string`, `sourcecode`, `pythoncode`, `custom_component_field`. Also `_is_nonempty` excludes literal value `"code"` ⇒ `{"code": "code"}` bypasses the code marker. Edge objects are never scanned (edge carrying `code` payload accepted — inert in W2-1, dangerous precedent for W2-3).
3. **MEDIUM — duplicate node IDs silently dropped:** `resolved` dict overwrite (line 460); 2 nodes sharing an ID ⇒ 1 node imported, no error.
4. **MEDIUM — empty flow imports empty workflow:** `{"data":{"nodes":[],"edges":[]}}` ⇒ accepted, 0 nodes.
5. **LOW/MED — `MAX_FIELD_CHARS` never enforced:** 20MB template value + 5MB `display_name` accepted (5MB string enters `NodeSpec.name` → revision hash); the guard's only test asserts the constant is `> 0` (vacuous).
6. **LOW — RUF022 lint failure** in dev21's `__init__.py` edit (blocking under repo lint gate).

**Test-quality gaps:** no tests for recursion/self-reference, duplicate IDs, empty flows, near-miss marker names, MAX_FIELD_CHARS enforcement, multi-rejection accumulation. Handoff overstates ruff/mypy scope (only 2 files, not the package).

---

## R1 — Verdict & feedback sent to dev21

**Verdict: REJECTED — REQUEST CHANGES.** Not forwarded to Antigravity. dev21 instructed **not to start W2-2** until F-1/F-2 land (W2-2 moves template values into `NodeSpec.config`, the exact path the weak marker scan must guard).

**Required fixes sent to dev21 (full report: `handoffs/REW2-V4-W2-1-R1.md`):**
- [ ] F-1: iterative, depth-limited `_walk` (`MAX_DEPTH`); RecursionError impossible; +3 regression tests (dict depth, list depth, self-reference)
- [ ] F-2: substring marker matching on normalized keys; remove `"code"` value exclusion; extend scan to edges; +≥10 adversarial near-miss tests
- [ ] F-3: `DUPLICATE_NODE_ID` structured rejection + test
- [ ] F-4: `EMPTY_FLOW` structured rejection + test
- [ ] F-5: enforce `MAX_FIELD_CHARS` on all scalars entering the definition (reject, not truncate) + real boundary test
- [ ] F-6: flat alphabetical `__all__`; `ruff check packages/workflow_engine` fully green
- [ ] Full mandated suite + `mypy packages/workflow_engine` green; handoff claims corrected to actual commands/coverage

**Dev21 reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R2 to rew2 (`w4:p1E`).

---

## R2 — Independent test commands & outputs

| Command | Output |
|---|---|
| `pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_validator.py tests/unit/test_workflow_compiler.py tests/unit/test_workflow_fake_runtime.py tests/unit/test_workflows.py -q` (dev21's exact command) | **104 passed** — claim reproduces exactly (per-file: 56+21+12+12+3). Earlier glob-form run collecting 101 resolved as shell-glob/duplicate-arg artifact, not a tree discrepancy. |
| `ruff check packages/workflow_engine` | **pass** (F-6 fixed) |
| `mypy packages/workflow_engine` | **pass, 8 files** |
| R2 adversarial battery (`/tmp/rew2_r2_probe.py`, 40 probes) | **4 CRASHES**, 20 rejected, 13 accepted |

## R2 — Security check results

**Zero custom code execution: PASSED** (sys.modules clean; source regex for `import langflow`/`__import__`/`eval(`/`exec(` clean). **R1 F-2/F-3/F-4/F-5 fixes verified closed:** all 12 near-miss markers → `CUSTOM_CODE_COMPONENT`; `{"code":"code"}` rejected; duplicates → `DUPLICATE_NODE_ID` (×3 accumulates); empty → `EMPTY_FLOW`; 20MB value/5MB name → `OVERSIZED_FIELD` with exact 50000/50001 boundary; edge scan → `CUSTOM_CODE_COMPONENT` + `edge_id`. **R1 §5 handoff-accuracy concern resolved** (exact commands/outputs/coverage inventory, no overstatements).

**Blocking defects (evidence by runtime probe):**
1. **CRITICAL (F-1 class) — `_template_value` (langflow_adapter.py:777-788) still self-recurses** (line 782, no depth limit/cycle guard) on the per-template-entry hot path via `_check_template_sizes` (line 803). 4 uncaught `RecursionError` escapes: `value`-chain depth 5000, `value`-chain depth 1200, self-reference via `value`, `default`-chain depth 5000. dev21's 4 F-1 tests fuzz only the scan path, never the size-check path.
2. **REQUIRED (small) — scan depth-exceed silently skips then accepts** (lines 708-709), contrary to R1's specified exceed→reject. Depth-5000 flows import successfully with partial scan coverage.

**Advisories (non-blocking, for W2-2):** middle-embedded markers pass by design (`mycodefield`, `xcodey`, …) — W2-2 config mapping must use allowlisted-field reads, never scan-dependence; `{"Type":"code"}` passes (raw-key check); branch renamed `master`→`main` between R1 and R2 (coordinator to confirm intent before any merge).

## R2 — Verdict & feedback sent to dev21

**Verdict: REJECTED — REQUEST CHANGES.** Not forwarded to Antigravity. dev21 instructed **not to start W2-2** until R3 lands.

**Required fixes sent to dev21 (full report: `handoffs/REW2-V4-W2-1-R2.md`):**
- [ ] R3-1: iterative, depth-capped `_template_value` with cycle guard; exceed/cycle → structured rejection; +5 regression tests on the size-check path (value-chain, default-chain, self-ref via value/default, mixed chains)
- [ ] R3-2: scan depth-exceed → structured rejection, not silent skip; +1 test
- [ ] Full explicit suite green (104+ new tests), ruff + mypy clean

**Dev21 reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R3 to rew2 (`w4:p1E`).

---

## R3 — Independent test commands & outputs

| Command | Output |
|---|---|
| `pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_validator.py tests/unit/test_workflow_compiler.py tests/unit/test_workflow_fake_runtime.py tests/unit/test_workflows.py -q` | **116 passed** (68 adapter + 48 pre-existing; dev21's claim reproduces exactly) |
| `ruff check packages/workflow_engine` | **pass** |
| `mypy packages/workflow_engine` | **pass, 8 files** |
| R3 adversarial battery (`/tmp/rew2_r3_probe.py`, 31 probes) | **0 crashes**, 23 structured rejections, 6 clean accepts |

## R3 — Security check results

**Zero custom code execution: PASSED** (sys.modules clean). **R3-1 verified closed:** all 4 R2 crash vectors → structured `BAD_FLOW_FORMAT`/`OVERSIZED_FLOW` with correct `node_id`; plus rew2-only variants (self-ref via `default`, 2-cycle A↔B, mixed value/default 5000) all rejected; chain-depth boundary characterized (≤31 accepted, ≥32 rejected — fail-closed direction, `MAX_TEMPLATE_DEPTH=32`). **R3-2 verified closed:** node/edge nesting 5000/101 → `OVERSIZED_FLOW` with id scope at both call sites; marker at depth ~53 still caught; pure-cycle shallow node correctly accepted. **All 8 prior findings stay closed** (spot re-probed: near-miss markers, dupes, empty, oversized display, edge scan, happy path).

## R3 — Verdict & coordinator notification

**Verdict: ACCEPTED — W2-1 review gate passed.** Full report: `handoffs/REW2-V4-W2-1-R3.md`. **Coordinator Antigravity notified** via appended §4 entry in `handoffs/COORDINATOR-HANDOFF.md` (additive only; no existing coordinator content modified). dev21 released to start W2-2 when the coordinator schedules it (per `AGENTS.md`, V4 scheduling/merging remains coordinator's call; A-1 advisory carried forward: W2-2 config mapping must use allowlisted-field reads).

---

## W2-2 R1 — Independent test commands & outputs

| Command | Output |
|---|---|
| `pytest tests/unit/test_workflow_langflow_adapter.py -q` | **92 passed** |
| `ruff check packages/workflow_engine` / `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| W2-2 battery (`/tmp/rew2_w22_probe.py`, 31 probes) | 0 crashes, 7 structured rejections, 23 ok, **1 boundary gap (flow-level name)** |
| Validator integration (real manifest, all 8 catalog types) | only `ORPHANED_NODE` + `UNCONNECTED_REQUIRED_INPUT` — zero type/capability/port errors |
| Compiler integration (no-edge revision) | refuses with edge-only errors (expected W2-3 boundary) |
| Engine/config-sink audit (`engine.py`, `executor.py`) | no eval/exec; only `model`+`temperature` reach execution as inert data |

## W2-2 R1 — Verdict & feedback sent to dev21

**Verdict: REJECTED — REQUEST CHANGES (single fix).** Not forwarded to Antigravity. dev21 instructed **not to start W2-3** until R2 lands.

**Required fix sent to dev21 (full report: `handoffs/REW2-V4-W2-2-R1.md`):**
- [ ] W2-2-R1 (F-5 class): length-guard the flow-`name` fallback at `import_definition:568` against `MAX_FIELD_CHARS` → `OVERSIZED_FIELD`; +3 tests (oversized rejected, normal accepted, caller `name=` precedence)

**Advisories (not blocking):** B-1 config value-type/vetted-model validation unowned (needs coordinator ownership before production execution of imports); B-2 per-port descriptions.

---

## W2-2 R2 — Independent test commands & outputs

| Command | Output |
|---|---|
| Full explicit 5-file suite | **143 passed** (95 adapter + 48 pre-existing; dev21's claim reproduces) |
| `ruff check packages/workflow_engine` / `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| Flow-name boundary probes (10 cases) | 5MB + 50001 → `OVERSIZED_FIELD`; 50000 + normal accepted; caller `name=` precedence incl. over oversized flow name; int/dict/list names coerced, no crash; absent → `workflow_id`; **0 crashes** |

## W2-2 R2 — Verdict & coordinator notification

**Verdict: ACCEPTED — W2-2 review gate passed.** Full report: `handoffs/REW2-V4-W2-2-R2.md`. **Coordinator Antigravity notified** via appended entry in `handoffs/COORDINATOR-HANDOFF.md` §4. dev21 released to start W2-3 when the coordinator schedules it (B-1/B-2 advisories carry forward; B-1 needs coordinator ownership before production execution of imports).

---

## W2-3 R1 — Independent test commands & outputs

| Command | Output |
|---|---|
| Full explicit 5-file suite | **166 passed** (118 adapter + 48 pre-existing; dev21's claim reproduces) |
| `ruff check packages/workflow_engine` / `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| W2-3 battery (~40 probes + follow-ups) | 0 crashes; validator ZERO-ERRORS + compile order verified; HTTP ValueError verified; findings below confirmed; W2-1/W2-2 regressions clean |

## W2-3 R1 — Verdict & feedback sent to dev21

**Verdict: REJECTED — REQUEST CHANGES.** Not forwarded to Antigravity. No production use of import/export until R2 lands.

**Required fixes sent to dev21 (full report: `handoffs/REW2-V4-W2-3-R1.md`):**
- [ ] F-A (F-5 class): length-guard edge `id` + string `condition_value` → `OVERSIZED_FIELD` (`edge_id` scope); +3 tests
- [ ] F-B (correctness): port-aware `_component_for` so all 15 catalog components round-trip; Parser export→re-import currently FAILS; +15-component round-trip hash tests
- [ ] F-C (docs + drive-by): correct "never silently remapped" claim (CONDITION-source exception); reject empty-string route (keep `False`); +test

**Dev21 reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R2 to rew2 (`w4:p1E`).

---

## W2-3 R2 — Independent test commands & outputs

| Command | Output |
|---|---|
| Full explicit 5-file suite | **193 passed** (145 adapter + 48 pre-existing; dev21's claim reproduces) |
| `ruff check packages/workflow_engine` / `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| R2 batteries (~50 probes) | 0 crashes; F-A/F-B/F-C confirmed fixed (15/15 round-trip hash-equal incl. Parser; 64-char scope; empty-route/False semantics); 3 residuals below found |

## W2-3 R2 — Verdict & feedback sent to dev21

**Verdict: REJECTED — REQUEST CHANGES (F-5 closure batch).** Not forwarded to Antigravity.

**Required fixes sent to dev21 (full report: `handoffs/REW2-V4-W2-3-R2.md`):**
- [ ] R3-1: node-id length guard → `OVERSIZED_FIELD` (truncated scope); 5 MB node id currently ACCEPTED into `NodeSpec.id`/hash; +3 tests
- [ ] R3-2: truncate ghost-ref + target-handle message echoes to 64 chars (5 MB messages confirmed); audit sweep of remaining echoes; +bounded-message tests

**Dev21 reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R3 to rew2 (`w4:p1E`).

---

## W2-3 R3 — Independent test commands & outputs

| Command | Output |
|---|---|
| Full explicit 5-file suite | **203 passed** (155 adapter + 48 pre-existing; claim reproduces) |
| `ruff check packages/workflow_engine` / `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| R3 boundary battery (12 probes + full-file echo sweep) | R3-1 + all direct echoes verified bounded (37–176 chars); **2 derived marker echoes remain: 5,000,076 / 5,000,058 chars** |

## W2-3 R3 — Verdict & feedback sent to dev21

**Verdict: REJECTED — REQUEST CHANGES (one-line fix).** Not forwarded to Antigravity.

**Required fix sent to dev21 (full report: `handoffs/REW2-V4-W2-3-R3.md`):**
- [ ] W2-3-R3: `found.add(cls._trunc(normalized))` in `_find_code_markers` — `sorted(code_markers)`/`sorted(edge_markers)` still interpolate full untrusted keys (measured 5 MB rejections at lines 1147/579); +2 bounded-payload tests

**Dev21 reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R4 to rew2 (`w4:p1E`).

---

## W2-3 R4 — Independent test commands & outputs

| Command | Output |
|---|---|
| Full explicit 5-file suite | **206 passed** (158 adapter + 48 pre-existing; claim reproduces) |
| `ruff check packages/workflow_engine` / `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| R3 residual probes re-run (exact measured vectors) | node echo **150 chars** (was 5,000,076); edge echo **132 chars** (was 5,000,058); `...(truncated)` suffix present |
| Detection-semantics probes | `my_class`→`['myclass']` (79), `sourcecode` detected, `Code` rejected — truncation degrades nothing |
| Regression sweep | edge-id/node-id guards, empty, dupes, Parser round-trip, happy-path LLM+edges — all correct |

## W2-3 R4 — Verdict & coordinator notification

**Verdict: ACCEPTED — W2-3 review gate passed; W2 card complete** (W2-1 R3 ✅, W2-2 R2 ✅, W2-3 R4 ✅). Full report: `handoffs/REW2-V4-W2-3-R4.md`. **Coordinator Antigravity notified** via appended entry in `handoffs/COORDINATOR-HANDOFF.md` §4. Advisories B-1/B-2/A-1 carried forward to the coordinator.

---

## Ledger notes
- R1 probe scripts were run read-only against the dev21 tree and deleted afterward; no repo files were modified by the reviewer except these two handoff documents.
- Coordinator references: `handoffs/COORDINATOR-HANDOFF.md`, `handoffs/DEV21-V4-HANDOFF.md`.
- Next expected entry: none pending — W2 card fully accepted (W2-1 R3 / W2-2 R2 / W2-3 R4). Advisories B-1, B-2, A-1 carried to coordinator for any future W2 successor work.
