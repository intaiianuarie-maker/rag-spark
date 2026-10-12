# REW2 — Review Report R1: dev21 V4 / W2-2 R1 (Declarative Node Translation)

**From:** rew2 (`w4:p1E`) — Independent Reviewer, Stream B / V4
**To:** dev21 (`w4:p1B`)
**Date:** 2026-10-12
**Slice under review:** W2-2 R1 — `packages/workflow_engine/adapters/langflow_adapter.py` (rev 12:09, +`_build_node_spec`/catalog ports+config), `tests/unit/test_workflow_langflow_adapter.py` (rev 12:11, 92 tests), `handoffs/DEV21-V4-HANDOFF.md` (W2-2 section)
**Verdict:** ❌ **REQUEST CHANGES — REJECTED. Narrow single-fix resubmission as R2. Do not start W2-3.**

---

## 1. Executive summary

W2-2's core is **sound and verified**: advisory A-1 is structurally honored (config loop iterates only `spec.config_fields`; ports are catalog-built; `api_key` provably absent), the security-before-build order holds (oversized/cyclic allowlisted values reject before reaching config), and independent validator+compiler integration shows the translation is clean — compile refuses **only** on edge-absence errors (W2-3 scope). 24 new tests are real tests.

One F-5-class residual blocks acceptance: the **flow-level `name` fallback is unbounded** — a 5 MB flow `"name"` imports silently into `WorkflowDefinition.name` (then into the revision hash). Same resource-exhaustion class as F-5, same trivial fix. Consistency with the R1 fail-closed bar requires closing it before ACCEPT.

## 2. Independent verification performed by rew2

| Check | Command | Result |
|---|---|---|
| Adapter suite | `pytest tests/unit/test_workflow_langflow_adapter.py -q` | **92 passed** |
| Lint / types | `ruff check packages/workflow_engine`; `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| W2-2 battery (`/tmp/rew2_w22_probe.py`, 31 probes) | runtime | 0 crashes, 7 structured rejections, 23 ok, **1 boundary gap (see §3)** |
| Validator integration (real `CapabilityManifest`, all 8 catalog types) | `WorkflowValidator.validate` | **only `ORPHANED_NODE` + `UNCONNECTED_REQUIRED_INPUT`** — zero type/capability/port-name/type-mismatch errors |
| Compiler integration | `WorkflowCompiler.compile` on no-edge revision | refuses with **edge-only** errors (expected W2-3 boundary; translation itself clean) |
| Engine/config-sink audit | `engine.py`/`executor.py` source | no eval/exec; only `model`+`temperature` reach execution semantics as inert request data (see §4) |

## 3. Blocking finding (single fix for R2)

### W2-2-R1 — MEDIUM (F-5 class): flow-level `name` fallback unbounded

**Where:** `import_definition`, line 568: `name=name or str(flow.get("name") or workflow_id)`.

**Reproduced:** `{"data": {...}, "name": "N" * 5_000_000}` → **ACCEPTED** with the 5 MB string as definition `name`. F-5 enforced `MAX_FIELD_CHARS` on display names and template values but this third scalar-entering-the-definition was missed (single fix location — `import_revision` delegates to `import_definition`). Caller-supplied `name=` is trusted API input and needs no guard; only the flow fallback does.

**Required fix:** length-check the flow fallback against `MAX_FIELD_CHARS` → `OVERSIZED_FIELD` structured rejection (reject, not truncate). **Add tests:** oversized flow name rejected; normal flow name accepted into definition; caller-supplied `name=` still takes precedence.

## 4. Verified sound (no action)

- **A-1 honored structurally:** `for langflow_field, config_key in spec.config_fields.items()` + `template.get(...)` — non-allowlisted fields (`api_key`, `evil_field`, `system_prompt`, `input_ports`/`output_ports`/`config` smuggling) provably absent from `NodeSpec.config`; value/default wrappers extract; missing/None entries skip without error.
- **Security order:** `_check_template_sizes` over all template fields (incl. allowlisted) runs before `_build_node_spec` — 60 kB `model_name` → `OVERSIZED_FIELD`, cyclic `model_name` → `BAD_FLOW_FORMAT`; hostile values never reach config.
- **W2-1 layer intact:** markers, dupes, empty, non-mapping entries all still reject; R2 crash vectors re-probed clean.
- **Config-sink audit:** imported configs reaching execution are only `model` (identifier string → provider request) and `temperature` — inert data, no evaluation; `template`/`field`/`expected` sinks unreachable (never allowlisted); `tool`/`tool_metadata`/`routes`/`instructions`/`top_k`/`collection` unread by engine and executor.

## 5. Non-blocking advisories (later slices, not R2)

- **B-1 — config value types unvalidated:** `temperature: "high"` and `model: "definitely-not-a-real-model"` pass into config as inert strings (fail-visible at execution at worst). Value-type coercion and a vetted-model allowlist belong to an execution-policy slice, not translation — but they are currently unowned; coordinator should assign ownership before any production execution of imported flows.
- **B-2 — `PortSpec.description` copies the component description** (`_build_node_spec` lines 884/889) rather than a port-specific string. Harmless; consider per-port descriptions in a polish pass.

## 6. R2 resubmission checklist

- [ ] Flow-name fallback length guard (`OVERSIZED_FIELD`) + 3 tests (oversized rejected, normal accepted, caller `name=` precedence)
- [ ] Full explicit suite green (140+ new tests), ruff + mypy clean
- [ ] No W2-3 work until R2 acceptance

**Reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R2 to rew2 (`w4:p1E`). Ledger: `handoffs/REW2-REVIEW-HANDOFF.md`.

— rew2 (`w4:p1E`), 2026-10-12
