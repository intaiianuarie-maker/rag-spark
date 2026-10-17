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
| W2-2 R2 | Declarative node translation (TRIGGER, RETRIEVER, LLM, OUTPUT) | ✅ **ACCEPTED by rew2** (2026-10-12) |
| W2-3 R4 | Edge/condition routing mapping (CONDITION, TRANSFORM, AGENT) + round-trip export | ✅ **ACCEPTED by rew2** (2026-10-12) |

> **W2 CARD COMPLETE** — all three slices accepted by the rew2 gate. Acceptance
> certifies slice quality only; merge and successor scheduling remain the
> coordinator's decision per `AGENTS.md`.

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
# 166 passed in 0.98s

# Adapter-only (W2-1 + W2-2)
/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py -q
# 118 passed in 0.59s

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
- **W2-2 node translation** (27 tests): TRIGGER (4), RETRIEVER (4), LLM (5), OUTPUT (4), config extraction forms (4), validator integration (3), flow name guard (3)
- **W2-3 edge+condition+export** (23 tests): edge translation (7), condition routing (4), validator/compile integration (3), round-trip export (8)

Known coverage gaps (documented honestly): middle-embedded markers
(e.g. `mycodefield`) are accepted by design (rew2 advisory A-1, deferred to W2-2
design). The W2-2 config mapping reads only explicitly allowlisted template
fields and does not depend on the scan for safety — closed by design in the
W2-2 slice. B-1 (config value-type/vetted-model validation) remains unowned and
needs coordinator assignment before production execution of imported flows;
B-2 (per-port descriptions) is a polish item.

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

---

## W2-3: Edge / Condition Routing Mapping + Round-Trip Export

### What W2-3 Delivers

1. **Edge translation** (`_build_edges`): Langflow edges → typed
   `WorkflowEdgeSpec`. Handle names resolved case-insensitively against
   catalog-declared ports (priority: `data.{side}Handle.name` → `{node}|{port}|{name}`
   string form; single-declared-port fallback only when handle metadata is
   absent entirely). A present-but-unmatched handle on a **non-CONDITION**
   source is rejected (`UNRESOLVED_SOURCE_PORT` / `UNRESOLVED_TARGET_PORT`).
   **CONDITION sources are a deliberate exception**: a route handle carries an
   opaque route label (`true`/`false`/route name), not a port name, so an
   unmatched handle resolves to the fixed `message` passthrough output port.
   The target port is still resolved strictly, so no port confusion is
   possible and the route label is inert data. (Corrected per rew2 F-C.)
2. **Condition routing**: CONDITION (ConditionalRouter) edges derive
   `condition_value` (priority: explicit `condition_value`/`condition`/`route`
   edge data → source handle name → target handle name; "true"/"false" coerced
   to bool). Route handles carry route values, not port names — data flows via
   the `message` passthrough port. Underivable routes are rejected
   (`CONDITION_EDGE_NO_VALUE`); empty-string routes are also rejected while
   falsy-but-meaningful `False` remains valid (per rew2 F-C).
3. **Round-trip export** (`export_definition` / `export_revision`):
   Langflow-1.x-shaped JSON; re-import reproduces the identical definition
   (same content hash) for **every** one of the 15 catalog components.
   Component selection is port-**and**-description aware so components sharing
   a `NodeType` (ChatInput/TextInput, Prompt/Template/Parser) each export to a
   component that reproduces them. Export metadata (revision number/hash) is
   ignored on re-import.
4. **Edge scalar guards** (per rew2 F-A): edge `id` and string
   `condition_value` are length-checked against `MAX_FIELD_CHARS` →
   `OVERSIZED_FIELD` (reject, not truncate). Structured rejections echo at
   most 64 chars of an id so an oversized id cannot become an unbounded error
   payload.

### Changed / Created Files (W2-3)

| File | Change |
|------|--------|
| `packages/workflow_engine/adapters/langflow_adapter.py` | Added `_build_edges`, `_edge_ref`, `_handle_name`, `_match_port`, `_condition_value`, `_CANONICAL_COMPONENT` map, real `export_definition`/`export_revision`, `_component_for`, `_template_for`, `_export_edge`; wired edge translation into `import_definition` |

### W2-3 Test Coverage (23 tests)

- **Edge translation** (7 tests): port mapping, handle-string resolution, single-port fallback, multi-input-port rejection (UNRESOLVED_TARGET_PORT), unknown-node rejection, missing-endpoint rejection
- **Condition routing** (4 tests): boolean coercion, passthrough port, named routes as strings, explicit condition_value priority, no-metadata rejection
- **Validator/compile integration** (3 tests): full LLM flow validates (0 errors), compiles with correct order, condition flow compiles with both routes
- **Round-trip export** (8 tests): content-hash equality, node/edge spec equality, config preserved, exported flow passes security scan, export structure, revision metadata, condition round-trip, unmapped-node-type ValueError

### W2-3 Test Commands & Results (exact commands run)

```bash
/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_validator.py tests/unit/test_workflow_compiler.py tests/unit/test_workflow_fake_runtime.py tests/unit/test_workflows.py -q
# 166 passed in 0.98s

/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py -q
# 118 passed in 0.59s

/root/ko-venv/bin/ruff check packages/workflow_engine/
# All checks passed!

/root/ko-venv/bin/mypy packages/workflow_engine/
# Success: no issues found in 8 source files
```

### Submission Status to rew2

- **W2-3**: submitted 2026-10-12 via `herdr agent prompt rew2` (pane w4:p1E)
- **W2-1 R3**: ✅ ACCEPTED · **W2-2 R2**: ✅ ACCEPTED

---

## W2-3 R2: re2 review findings (F-A, F-B, F-C) fixed

| Finding | Fix | Tests |
|---------|-----|-------|
| F-A (F-5 class, MEDIUM): 5 MB edge `id` / `condition_value` entered spec + revision hash | Length guards on edge `id` and string `condition_value` → `OVERSIZED_FIELD` with `edge_id` scope; rejection echoes ≤64 chars of an id so the error payload stays bounded | 5 (oversized id, oversized route, id at-limit accepted, route at-limit accepted, bounded rejection scope) |
| F-B (correctness, MEDIUM): Parser exported as `prompt`, re-import failed `UNRESOLVED_TARGET_PORT` | Port-**and**-description-aware `_component_for`; per-`NodeType` canonical only as fallback | 19 (parametrized round-trip for all 15 catalog components, Parser→`parser`, Prompt→`prompt`, Parser wired through edges) |
| F-C (LOW): blanket "never silently remapped" claim was false for CONDITION sources; empty-string routes accepted | Docstring + handoff corrected to document the CONDITION passthrough exception as design necessity; empty-string routes now rejected while `False` survives | 2 (empty `condition_value` rejected, no-metadata route rejected, `False` accepted) |

### W2-3 R2 Test Commands & Results (exact commands run)

```bash
/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_validator.py tests/unit/test_workflow_compiler.py tests/unit/test_workflow_fake_runtime.py tests/unit/test_workflows.py -q
# 193 passed in 1.26s

/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py -q
# 145 passed in 0.70s

/root/ko-venv/bin/ruff check packages/workflow_engine/
# All checks passed!

/root/ko-venv/bin/mypy packages/workflow_engine/
# Success: no issues found in 8 source files
```

### Submission Status to rew2 (W2-3)

- **W2-3 R1**: REJECTED (F-A, F-B, F-C)
- **W2-3 R2**: resubmitted 2026-10-12 via `herdr agent prompt rew2` (pane w4:p1E)

---

## W2-3 R3: F-5 closure batch (R3-1 node-id guard, R3-2 bounded echoes)

| Finding | Fix | Tests |
|---------|-----|-------|
| R3-1 (F-5 class, MEDIUM): unbounded node id entered `NodeSpec.id`, internal dict keys, edge endpoint refs, rejection echoes and the revision hash | Node id length-guarded at ingestion (mirrors the display-name guard) → `OVERSIZED_FIELD`, scope truncated | 4 (oversized rejected, normal unaffected, at-limit accepted, oversized edge reference rejected with bounded echo) |
| R3-2 (LOW/MED): 2 unbounded error-message echoes (ghost node ref, unmatched target handle) — GB-scale log amplification across N hostile edges | New `_trunc` helper bounds every user-controlled string echoed into a rejection; applied to node/edge ids, ghost refs, target/source handles, component types, template field names, and all message strings | 6 (ghost ref, unmatched handle, oversized component type, oversized field name, duplicate oversized id, `_trunc` helper) |

Audit sweep: every rejection message/scope field that embeds untrusted input now
goes through `_trunc` (`_REJECTION_ID_CHARS = 64`, `...(truncated)` suffix).

### W2-3 R3 Test Commands & Results (exact commands run)

```bash
/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_validator.py tests/unit/test_workflow_compiler.py tests/unit/test_workflow_fake_runtime.py tests/unit/test_workflows.py -q
# 203 passed in 1.12s

/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py -q
# 155 passed in 0.91s

/root/ko-venv/bin/ruff check packages/workflow_engine/
# All checks passed!

/root/ko-venv/bin/mypy packages/workflow_engine/
# Success: no issues found in 8 source files
```

### Submission Status to rew2 (W2-3 R3)

- **W2-3 R1**: REJECTED (F-A, F-B, F-C) · **W2-3 R2**: REJECTED (R3-1, R3-2)
- **W2-3 R3**: resubmitted 2026-10-12 via `herdr agent prompt rew2` (pane w4:p1E)

---

## W2-3 R4: derived marker-set echoes bounded

| Finding | Fix | Tests |
|---------|-----|-------|
| W2-3-R3 (LOW/MED): `found.add(normalized)` stored the full-length untrusted marker key; two message sites interpolated `sorted(markers)` raw → 5,000,076-char (node) and 5,000,058-char (edge) rejection messages | Bound at insertion: `found.add(cls._trunc(normalized))`. Matching already runs on the full string before insertion, so only the echoed representation shrinks; set truthiness and detection are unaffected | 3 (huge marker key on node bounded, huge marker key on edge bounded, ordinary marker key still reported exactly as `myclass`) |

Rew2's full-file sweep confirmed these were the only two remaining unbounded
interpolations in the module.

### W2-3 R4 Test Commands & Results (exact commands run)

```bash
/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_validator.py tests/unit/test_workflow_compiler.py tests/unit/test_workflow_fake_runtime.py tests/unit/test_workflows.py -q
# 206 passed in 1.47s

/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py -q
# 158 passed

/root/ko-venv/bin/ruff check packages/workflow_engine/
# All checks passed!

/root/ko-venv/bin/mypy packages/workflow_engine/
# Success: no issues found in 8 source files
```

### Submission Status to rew2 (W2-3 R4)

- **W2-3**: R1 REJECTED → R2 REJECTED → R3 REJECTED → **R4 resubmitted 2026-10-12**

---

## FINAL — W2 Card accepted (2026-10-12)

**W2-3 R4: ✅ ACCEPTED.** rew2 re-measured the R3 vectors with the fix in place
(node echo 150 chars, edge echo 132 chars — down from 5,000,076 / 5,000,058),
confirmed detection semantics unchanged, and ran a regression sweep green.

### W2 card acceptance summary

| Slice | Verdict | Acceptance report |
|-------|---------|-------------------|
| W2-1 security boundary | ✅ ACCEPTED (R3) | `handoffs/REW2-V4-W2-1-R3.md` |
| W2-2 typed ports + allowlisted config | ✅ ACCEPTED (R2) | `handoffs/REW2-V4-W2-2-R2.md` |
| W2-3 edges/conditions + round-trip export | ✅ ACCEPTED (R4) | `handoffs/REW2-V4-W2-3-R4.md` |

Ledger: `handoffs/REW2-REVIEW-HANDOFF.md`. Coordinator notified by rew2 in
`handoffs/COORDINATOR-HANDOFF.md` §4 (W2-1 → W2-2 → W2-3, chronological).

### Final deliverable state

**Files (workflow-only; no other package touched):**
- `packages/workflow_engine/adapters/__init__.py` (new)
- `packages/workflow_engine/adapters/langflow_adapter.py` (new)
- `packages/workflow_engine/__init__.py` (modified — additive W2 exports)
- `tests/unit/test_workflow_langflow_adapter.py` (new — 158 tests)
- `handoffs/DEV21-V4-HANDOFF.md` (this document)

**Delivered capability:** pure, restricted Langflow ⇄ product translator.
Restricted declarative allowlist (15 components) mapped to product node types
with typed `PortSpec` contracts; import produces `WorkflowDefinition` /
immutable `WorkflowRevision`; port-level `WorkflowEdgeSpec` edges with
CONDITION route values; export round-trips hash-identically for every catalog
component. Custom Python components, unvetted module paths, and code-bearing
templates are rejected fail-closed with structured errors. The adapter never
installs, imports, or executes Langflow or any component code.

**Final verification (last run):**
```bash
/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_validator.py tests/unit/test_workflow_compiler.py tests/unit/test_workflow_fake_runtime.py tests/unit/test_workflows.py -q
# 206 passed in 1.47s
/root/ko-venv/bin/ruff check packages/workflow_engine/     # All checks passed!
/root/ko-venv/bin/mypy packages/workflow_engine/            # Success: 8 source files
```

### Open items carried to the coordinator (not blocking)

- **B-1** — config value types are unvalidated (`temperature: "high"`, unvetted
  model names pass through as inert data). Needs an owner **before any
  production execution of imported flows**. This is an execution-policy slice,
  not translation.
- **B-2** — `PortSpec.description` copies the component description rather than a
  port-specific string (polish).
- **A-1** — the marker scan is defense-in-depth only; config safety comes from
  the explicit allowlist. Mid-embedded marker keys are accepted (keys never
  persist), acceptable by design.
- **Merge/scope** — the working tree also holds dev11's Stream A (V3) changes;
  merge must stay per-stream. Branch is `main` (was `master` at R1) — confirm
  intent. Per `AGENTS.md`, only the coordinator merges or schedules successors.

---

## B-SLICE (post-W2): advisories B-1 and B-2 — IMPLEMENTED, awaiting rew2 review

**Scope:** `packages/workflow_engine/**` only. No Stream A / V1 / V2 files touched.

### B-1 — config value-type + vetted-model validation

Imported config values are now **validated, not passed through inert**. Rejections
use the existing structured-error channel (`INVALID_CONFIG_VALUE`, with `node_id`,
bounded echo) and fail closed — values are never coerced.

- `VETTED_MODELS` frozenset (`gpt-4o-mini`, `gpt-4o`, `gpt-4.1-mini`, `gpt-4.1`).
  A model outside the list is rejected, so an imported flow can never name an
  unvetted provider model. Ownership of this list belongs to the
  coordinator / execution-policy layer; the adapter only enforces membership.
- `_ConfigRule` + `_CONFIG_RULES` per product config key: `model` (vetted
  allowlist), `temperature` (number, 0.0–2.0), `max_tokens` (integer, 1–32768),
  `top_k` (integer, 1–1000), `collection`/`tool` (safe-identifier),
  `stream` (boolean), `tool_metadata` (dict), `instructions` (string),
  `routes` (list).
- `_is_safe_identifier` blocks whitespace, control chars, path traversal (`..`)
  and shell-ish metacharacters from name-like fields.
- `bool` is explicitly rejected for numeric fields (it subclasses `int`).
- Validation runs in the security pass (`_check_config_values`), so invalid
  values never reach `NodeSpec.config` or the revision hash.
- **A-1 still holds:** only allowlisted `config_fields` are inspected, so
  unknown template fields are neither validated nor copied.

### B-2 — per-port descriptions

`_IOPort` gained a `description` field; all 34 catalog ports across the 15
components now carry a port-specific description. `PortSpec.description` uses
the port's own description (falling back to the component description if a port
declares none), instead of copying the component blurb onto every port. The
component description still populates `NodeSpec.description`.

### Changed files

| File | Change |
|---|---|
| `packages/workflow_engine/adapters/langflow_adapter.py` | `_IOPort.description`; per-port descriptions for all 34 ports; `VETTED_MODELS`, `_ConfigRule`, `_CONFIG_RULES`, `_is_safe_identifier`, `_validate_config_value`, `_check_config_values`; `_build_node_spec` uses port descriptions; config validation wired into the security pass |
| `tests/unit/test_workflow_langflow_adapter.py` | +26 tests (B-1: 16, B-2: 10) |

### Test Commands & Results (exact commands run)

```bash
/root/ko-venv/bin/pytest tests/unit/test_workflow_langflow_adapter.py tests/unit/test_workflow_validator.py tests/unit/test_workflow_compiler.py tests/unit/test_workflow_fake_runtime.py tests/unit/test_workflows.py -q
# 232 passed in 1.60s   (184 adapter + 48 pre-existing; no regressions)

/root/ko-venv/bin/ruff check packages/workflow_engine/
# All checks passed!

/root/ko-venv/bin/mypy packages/workflow_engine/
# Success: no issues found in 8 source files
```

Integration spot-check: a vetted-model LLM flow still round-trips
hash-identically through export→import and validates clean against a real
`CapabilityManifest`.

### Test Coverage (26 new tests)

- **B-1 vetted models** (4): vetted accepted, unvetted rejected, message names
  the vetted set, `VETTED_MODELS` exported
- **B-1 value types/ranges** (13): temperature out-of-range / string / boolean
  rejected, in-range accepted; max_tokens non-integer / zero rejected, valid
  accepted; top_k negative rejected; collection path-traversal rejected, valid
  accepted; stream string rejected, boolean accepted; non-allowlisted fields not
  validated (A-1); rejection carries `node_id`; rejection echo bounded;
  `_validate_config_value` reason string
- **B-2 per-port descriptions** (9): distinct LLM port descriptions,
  port-specific retriever/condition/output descriptions, port description
  differs from component description, every catalog port has one

### Submission Status to rew2

- **B-SLICE**: submitted 2026-10-12 via `herdr agent prompt rew2` (pane w4:p1E) — awaiting review

### Notes for the reviewer

- `VETTED_MODELS` membership is the coordinator's policy input; the adapter
  enforces it but does not own the list. Widening it is a one-line change with
  the same review bar as widening the component catalog.
- B-1 is intentionally strict: it rejects rather than coerces, so an import
  that previously succeeded with `temperature: "high"` now fails closed with
  `INVALID_CONFIG_VALUE`. That is the intended behaviour change.

---

## B-slice R1 remediation — IMPLEMENTED, resubmitting as R2 to rew2

**Date:** 2026-10-13 (post-restart continuation by dev21)
**Trigger:** `handoffs/REW2-V4-B-R1.md` — verdict REQUEST CHANGES (1 HIGH crash,
1 HIGH fail-closed, 1 MEDIUM resource, 1 LOW claim-alignment).

### Findings and what changed

| # | Severity | Finding | Fix |
|---|---|---|---|
| B-R1-1 | HIGH | `temperature: 10**100000` crashed `import_definition` with a raw `ValueError` (CPython int→str limit) because rejection reasons echoed `{value!r}` unbounded. | New `_format_number()` echo helper: floats formatted `.10g`, non-finite by name, integers at/beyond a 32-digit ceiling reported as an approximate digit count computed via `bit_length()` (never converts the value). Integer kinds are also pre-rejected past `_INT_MAGNITUDE_CEILING = 10**32`, which also keeps the huge int away from `json.dumps` in the content hash. |
| B-R1-2 | HIGH | `temperature: NaN` was **accepted** into `NodeSpec.config` and the revision hash (`json.dumps` emits non-standard `NaN`); both range comparisons are False for NaN. | `math.isfinite()` check for number/integer kinds **before** range checks; NaN/±inf rejected with a bounded "must be a finite number" reason. |
| B-R1-3 | MEDIUM | `conditions: ["x"*10_000_000]` and `conditions: [NaN]` were accepted into config/hash — the list rule checked container type/count only. | List elements validated: each must be a `str` capped at `_MAX_STRING_CHARS` (256, the shared per-string cap), with the element index in the reason. |
| B-R1-4 | LOW | The `kind="dict"` rule for `tool_metadata` is unreachable from the import path (`_template_value` drops mapping payloads) while the code implied it was exercised. | Chose **document-and-test** over speculative extraction: `_template_value`, `_check_config_values` and the `_CONFIG_RULES` entry now state explicitly that dict-valued config fields are dropped from Langflow templates, the rule is kept as declared defence-in-depth for non-template paths, and a regression test pins the drop (including hash-neutrality). |

### Changed files

- `packages/workflow_engine/adapters/langflow_adapter.py` — `_format_number()`, `_INT_DIGIT_CEILING`/`_INT_MAGNITUDE_CEILING`, `math` import, rewritten number/integer branch, list-element validation, dict-drop documentation.
- `tests/unit/test_workflow_langflow_adapter.py` — 28 new tests across 4 classes (`TestBR1HugeIntegerNeverCrashes`, `TestBR1NonFiniteRejected`, `TestBR1RoutesElementValidation`, `TestBR1DictValuedFieldDropped`); plus 4 pre-existing lint defects fixed in the same file (unsorted import block, 3 unused unpacked variables, 2 stale `noqa: ANN001`).
- `tests/fixtures/workflows/langflow_restricted_roundtrip.json` — **new**: the W2 restricted round-trip fixture (trigger → LLM → conditional router → two outputs).
- `tests/unit/test_workflow_langflow_fixture.py` — **new**: 27 tests over that fixture (hygiene, security pass, W0/W1 validate+compile determinism, round-trip purity, out-of-subset rejection).
- `rag-fork-solution/V4-W2-SPIKE.md` — **new**: spike evidence, controls, scope honesty, verification commands, W3 handoff.
- `rag-fork-solution/NOTICES.md`, `rag-fork-solution/REUSE_MAP.md` — Langflow attribution/reuse rows (schema reference only, no dependency, no source copied).

### Test commands & results (exact)

```bash
cd "/root/code/Super Rag/knowledge-os"
PYTHONPATH=packages:apps/api:apps/worker:.:tests:tests/fixtures \
  /root/ko-venv/bin/python3 -m pytest \
  tests/unit/test_workflow_langflow_adapter.py \
  tests/unit/test_workflow_langflow_fixture.py \
  tests/unit/test_workflow_validator.py \
  tests/unit/test_workflow_compiler.py \
  tests/unit/test_workflow_fake_runtime.py \
  tests/unit/test_workflows.py -q
# → 287 passed   (212 adapter + 27 fixture + 48 pre-existing)

/root/ko-venv/bin/ruff check packages/workflow_engine \
  tests/unit/test_workflow_langflow_adapter.py \
  tests/unit/test_workflow_langflow_fixture.py
# → All checks passed!

/root/ko-venv/bin/python3 -m mypy packages/workflow_engine
# → Success: no issues found in 8 source files

PYTHONPATH=packages:apps/api:apps/worker:.:tests:tests/fixtures \
  /root/ko-venv/bin/python3 -m pytest tests/unit -q
# → 1039 passed  (whole suite, unrelated streams green)
```

### Reviewer probes this version answers

- `temperature: 10**100000` → structured `INVALID_CONFIG_VALUE` rejection, no exception, bounded payload (≤300 chars).
- `temperature: NaN` / `±inf` → rejected before any range check; never in config, never in `content_hash`.
- `conditions: ["x"*10_000_000]` / `[NaN]` / `["ok", 42]` → rejected with element index.
- `tool_metadata` dict payload → dropped (documented, hash-neutral, test-pinned).

### Notes / caveats for the reviewer

- `_INT_DIGIT_CEILING = 32` is a conversion-safety ceiling, not a product
  semantic: integers inside it are still range-checked as before, so
  `max_tokens: 10**31` still fails on range (verified by a dedicated test).
- B-R1-4 chose the "document" branch; if the coordinator prefers checked dict
  extraction, that is a follow-up card (it needs a definition of which nested
  keys are data vs. template metadata).
- No dependency, import or execution of Langflow was introduced anywhere.

### Submission status

Resubmitting to rew2 (`w4:p1E`) as **B-slice R2**: B-R1-1 … B-R1-4 closed,
28 adapter tests + 27 fixture tests + the four pre-existing lint defects.
