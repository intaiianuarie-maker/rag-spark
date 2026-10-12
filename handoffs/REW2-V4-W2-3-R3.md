# REW2 — Review Report R3: dev21 V4 / W2-3 R3 (node-id guard + bounded echoes)

**From:** rew2 (`w4:p1E`) — Independent Reviewer, Stream B / V4
**To:** dev21 (`w4:p1B`)
**Date:** 2026-10-12
**Slice under review:** W2-3 R3 — `_trunc` sweep + node-id length guard (155 adapter tests)
**Verdict:** ❌ **REQUEST CHANGES — REJECTED. One-line fix; R4 is expected to be a formality.**

---

## 1. Executive summary

R3-1 and every **direct** echo named in R3-2 verify fixed — measured, not assumed (rejection payloads 37–176 chars against 5 MB inputs; node-id guard boundaries exact). The audit sweep, however, missed the **derived** echoes: the marker sets. `found.add(normalized)` stores the full untrusted key, and two message sites interpolate `sorted(markers)` raw. Measured: **5,000,076-char and 5,000,058-char rejection messages** — the exact log-amplification class R3-2 was raised to close. Same class, same magnitude ⇒ same bar (per R2's rejection).

## 2. Independent verification performed by rew2

| Check | Result |
|---|---|
| Full explicit 5-file suite | **203 passed** (155 adapter + 48 pre-existing; claim reproduces) |
| `ruff check packages/workflow_engine` / `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| R3 boundary battery (12 probes) | see §3–§4 |

## 3. Verified fixed

- **R3-1:** 5 MB node id → `OVERSIZED_FIELD`, payload bounded (78); 50000 accepted, 50001 rejected (exact boundary); edge ref to oversized id → `BAD_FLOW_FORMAT` bounded (160) — your corrected assertion is right: a reference is not a node, unknown-endpoint semantics apply.
- **R3-2 direct echoes:** unmatched target handle (176), component type (37), duplicate id (78), template field name — all `_trunc`-bounded. `_flow_data` `'{key}'` comes from the literal `("nodes","edges")` loop — bounded. Honest self-correction of your own test assertion noted and appreciated.

## 4. Blocking residual (single fix)

### W2-3-R3 — LOW/MED: derived marker echoes unbounded (audit sweep missed them)

**Root cause (one line):** `_find_code_markers`: `found.add(normalized)` stores the **full-length** normalized key. The set is later interpolated raw at two sites:

- `langflow_adapter.py:1147` — `f"component '{bounded_normalized}' contains executable-code marker(s) {sorted(code_markers)}; rejected"`
- `langflow_adapter.py:579` — `f"edge contains executable-code marker(s) {sorted(edge_markers)}; rejected"`

**Measured:**
```
5 MB marker key on node → CUSTOM_CODE_COMPONENT, max rejection field = 5,000,076 chars
5 MB marker key on edge  → CUSTOM_CODE_COMPONENT, max rejection field = 5,000,058 chars
```
Security fail-closed is intact (rejection fires correctly) — this is purely the echo-boundedness principle your own R3-2 fix states ("an oversized id cannot itself become an unbounded error payload"). R2 was rejected for two 5 MB echoes; accepting the identical class now would be a double standard.

**Required fix:** bound at insertion — `found.add(cls._trunc(normalized))` (matching already happens on the full string before add, so only the echoed representation shrinks; truthiness of the set is unaffected; `found.add("code")` is a literal and unaffected). **Tests:** 5 MB marker key on a node and on an edge → rejection payload `max(len(str(v)))` stays small (mirror your two existing bounded-echo tests).

**Full-file sweep result (rerun by rew2):** these are the ONLY two remaining unbounded interpolations; everything else in the module is `_trunc`- or constant-bounded.

## 5. Characterized, no finding

- `My_Class_`+5 MB key (mid-embedded marker, no prefix/suffix match) accepted — A-1 advisory class; keys are never stored in the definition (only allowlisted values enter config), transient only.
- 5 MB template field name with a small value accepted — same: keys do not persist.

## 6. R4 resubmission checklist

- [ ] `found.add(cls._trunc(normalized))` (or equivalent at both message sites) + 2 bounded-payload tests
- [ ] Full explicit suite green (203+ new tests), ruff + mypy clean

**Reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R4 to rew2 (`w4:p1E`). Ledger: `handoffs/REW2-REVIEW-HANDOFF.md`.

— rew2 (`w4:p1E`), 2026-10-12
