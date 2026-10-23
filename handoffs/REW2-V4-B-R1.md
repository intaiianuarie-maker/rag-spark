# REW2 — Review Report R1: dev21 B-Slice (B-1 config validation + B-2 per-port descriptions)

**From:** rew2 (`w4:p1E`) — Independent Reviewer, Stream B / V4
**To:** dev21 (`w4:p1B`) (handoff files only) · **Coordinator:** museR
**Date:** 2026-10-12
**Slice under review:** B-slice — `VETTED_MODELS` + `_ConfigRule`/`_CONFIG_RULES` + `_validate_config_value`/`_check_config_values` + per-port descriptions (184 adapter tests; 232 suite)
**Verdict:** ❌ **REQUEST CHANGES — REJECTED. Resubmit as R2.**

---

## 1. Executive summary

B-2 is fully verified clean. B-1's framework is well-built (strict allowlist, identifier hardening, bool-vs-int handled, bounded echoes, correct security-pass ordering at line 748, A-1 preserved, round-trip hash-equal) — but three adversarial inputs break its stated contract, one of them with a **raw crash**. All measured, below.

## 2. Independent verification

| Check | Result |
|---|---|
| Full 5-file suite | **232 passed** (184 adapter + 48 pre-existing; claim reproduces) |
| `ruff check packages/workflow_engine` / `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| B-1/B-2 adversarial battery (31 probes) | 3 findings + 1 dead path confirmed (§3–§4) |
| B-2 catalog audit | **34/34 ports** carry descriptions, 20 distinct, 0 copying the component blurb — claim verified |

## 3. Blocking findings (R2 required)

### B-R1-1 — HIGH (never-crash invariant): huge-int config value crashes the import

**Where:** `_validate_config_value`, number-kind reason strings (`f"… got {value!r}"`).
**Reproduced:** `temperature: 10**100000` → **`ValueError: Exceeds the limit (4300 digits) for integer string conversion`** — an uncaught raw exception escaping `import_definition`, not a structured rejection. Python's `int_max_str_digits` guard turns the unbounded `{value!r}` echo into a crash path. Same class as W2-1 R1's F-1: hostile input must never crash the boundary.
**Required fix:** bound numeric formatting in rejection reasons (e.g. digit-capped conversion: format via `f"{value:.10g}"` for floats, and for ints use `str(value)[:64] + "…"` guarded against the conversion limit — or pre-reject any int beyond a digit ceiling before formatting). **Test:** 100k-digit config value → structured rejection, no exception.

### B-R1-2 — HIGH (fail-closed promise): NaN temperature is accepted

**Reproduced:** `temperature: NaN` (legal in Python-parsed JSON) → **ACCEPTED**, `{'temperature': nan}` in `NodeSpec.config` and the revision hash (`json.dumps` emits non-standard `NaN`). Range checks `value < 0.0` / `value > 2.0` are both False for NaN, so it slides through. B-1's headline claim is "invalid values never reach NodeSpec.config" — a non-finite float is invalid under any temperature semantics.
**Required fix:** for number/integer kinds, reject non-finite values (`math.isfinite`) before range checks. **Tests:** NaN rejected; +inf/-inf rejected (they currently fail via range, lock it in).

### B-R1-3 — MEDIUM (F-5 resource class): routes list elements unvalidated

**Reproduced:** `conditions: ["x" * 10_000_000]` → **ACCEPTED** — the 10 MB string enters `NodeSpec.config["routes"]` and the revision hash. `conditions: [NaN]` also accepted. The list rule checks type and count (≤1000) but nothing about element type or size — the exact resource class F-5 closed on every other path into the definition.
**Required fix:** validate elements — route labels are strings: reject non-str elements, cap element length (`_MAX_STRING_CHARS` or `MAX_FIELD_CHARS`, your call — state it). **Tests:** oversized element rejected; non-str element rejected; normal routes accepted.

## 4. Non-blocking finding

### B-R1-4 — LOW (dead validation path): dict-valued tool_metadata can never reach config

`_template_value` returns `None` for a Mapping without `value`/`default` keys, so `tool_metadata: {...}` templates are **silently dropped** — config shows `{}` (verified). The declared `kind="dict"` rule and its "accepted" semantics are unreachable through the import path; no test covers it end-to-end. Safe (dropping is fail-closed), but the rule/claim is misleading. **Fix:** document "dict-valued config fields are not extracted from Langflow templates (dropped)" or implement checked dict extraction — pick one; adjust claims/tests to match.

## 5. Verified sound (credit)

Model allowlist exact-match + bounded "allowed" listing; case/whitespace strictness (fail-closed direction); identifier hardening (`../etc/passwd`, `a b`, `db;rm -rf`, backticks all rejected; `docs_v1.2-3` accepted); bool rejected for numbers/`max_tokens`; `stream` strict-boolean; top_k/max_tokens boundaries exact (1/1000/32768 accepted, 0/1001 rejected); A-1 preserved (unknown fields neither validated nor copied); rejections carry `node_id` + bounded echoes on every non-crash path; vetted LLM flow round-trips hash-identically; B-2 fully clean; all W2 regressions green.

## 6. R2 resubmission checklist

- [ ] B-R1-1: bounded numeric formatting in rejection reasons + crash regression test
- [ ] B-R1-2: `math.isfinite` guard for number kinds + NaN/inf tests
- [ ] B-R1-3: routes element type/length validation + 3 tests
- [ ] B-R1-4: doc/claim alignment for dict-valued fields
- [ ] Full suite green (232+ new tests), ruff + mypy clean

**Reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R2 to rew2 (`w4:p1E`). Ledger: `handoffs/REW2-REVIEW-HANDOFF.md`.

— rew2 (`w4:p1E`), 2026-10-12
