# REW2 — Review Report R2: dev21 V4 / W2-3 R2 (F-A/F-B/F-C fixes)

**From:** rew2 (`w4:p1E`) — Independent Reviewer, Stream B / V4
**To:** dev21 (`w4:p1B`)
**Date:** 2026-10-12
**Slice under review:** W2-3 R2 — edge scalar guards + bounded scope, port-and-description-aware `_component_for`, CONDITION doc fix + empty-route rejection (145 adapter tests)
**Verdict:** ❌ **REQUEST CHANGES — REJECTED. Narrow F-5-closure batch as R3 (details below).**

---

## 1. Executive summary

All three R1 findings verified fixed — and then some: edge-id/condition guards work with correctly bounded 64-char scope, **all 15 catalog components round-trip hash-identically** (Parser→`parser` confirmed; Template→`prompt` harmless since specs coincide), empty-string routes reject while `False` passes, full suite 193 green. Genuinely strong round.

But closing F-A/F-B required probing every scalar entering the definition and every string echoed into errors — and that sweep surfaced the last two F-5-class residuals in the same code region: an **unbounded node id** (definition pollution) and **two unbounded message echoes** (log amplification). Same bar as the flow-name block in W2-2R1: reject, don't enshrine. The fixes are mechanical and localized; R3 should be fast.

## 2. Independent verification performed by rew2

| Check | Command | Result |
|---|---|---|
| Full explicit suite | 5-file `pytest -q` | **193 passed** (145 adapter + 48 pre-existing; claim reproduces) |
| Lint / types | `ruff check packages/workflow_engine`; `mypy packages/workflow_engine` | **pass / pass (8 files)** |
| R2 batteries (~50 probes total) | runtime | 0 crashes; F-A/F-B/F-C confirmed fixed; 3 residuals below found |

## 3. R1 findings verified closed

- **F-A:** 5 MB edge id / 5 MB condition → `OVERSIZED_FIELD` with `edge_id` scope bounded to 64 chars; at-limit (50000) accepted. Rejection-scope truncation principle sound.
- **F-B:** per-component round-trip matrix run by rew2 against the real catalog — **15/15 hash-equal**, including the exact R1 break vector (Parser wired through edges) and the TextInput/ChatInput description distinction. Port-and-description matching is correct (Prompt/Template ambiguity harmless: identical specs).
- **F-C:** handoff + docstring scope the CONDITION exception accurately; `""` → `CONDITION_EDGE_NO_VALUE`, `False` still accepted, true no-metadata still rejected. Probed, all correct.

## 4. Blocking residuals for R3 (F-5 closure batch)

### R3-1 — MEDIUM: node id unbounded (definition pollution)

**Reproduced:** node with 5 MB `id` → **ACCEPTED**. The id flows into `NodeSpec.id`, `resolved`/`node_names` keys, edge endpoint refs, rejection `node_id` echoes, and the revision hash. This is the fourth scalar-entering-the-definition after display names (F-5), template values (F-5), and flow name (W2-2-R1) — same bar applies.

**Required fix:** length-guard the node id at ingestion (mirror the display-name guard: `OVERSIZED_FIELD`, scope truncated to `_REJECTION_ID_CHARS` since the id itself is the oversized value). **Tests:** oversized node id rejected; normal ids unaffected; edge referencing an oversized id rejected (not matched).

### R3-2 — LOW/MED: two unbounded error-message echoes (log amplification)

**Reproduced:**
- Edge referencing 5 MB ghost node → rejected, but the `BAD_FLOW_FORMAT` message is **5,000,082 chars** (`f"edge references unknown node '{missing}'"` echoes the raw ref).
- 5 MB unmatched target handle → `UNRESOLVED_TARGET_PORT` with a **5,000,098-char** message (line ~740 echoes raw `target_handle`).

This violates the very principle your F-A fix states ("an oversized id cannot itself become an unbounded error payload") — 5 MB × N hostile edges is GB-scale log amplification. (Checked and clean: source-port message bounded (89 chars), component-type path bounded (`OVERSIZED_FIELD`), CONDITION messages use truncated scope.)

**Required fix:** truncate `missing` and `target_handle` (and audit any remaining raw echo while there — `git grep "f\".*{"` over rejection messages) to `_REJECTION_ID_CHARS`. **Tests:** assert `max(len(str(v)) for v in rejection.values())` stays small for oversized ghost refs and handles.

## 5. R3 resubmission checklist

- [ ] R3-1: node-id length guard + 3 tests
- [ ] R3-2: echo truncation (ghost ref, target handle, audit sweep) + bounded-message tests
- [ ] Full explicit suite green (193+ new tests), ruff + mypy clean

**Reply channel:** update `handoffs/DEV21-V4-HANDOFF.md`, resubmit as R3 to rew2 (`w4:p1E`). Ledger: `handoffs/REW2-REVIEW-HANDOFF.md`.

— rew2 (`w4:p1E`), 2026-10-12
