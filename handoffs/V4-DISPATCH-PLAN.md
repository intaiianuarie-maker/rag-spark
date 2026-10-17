# V4 Dispatch Plan — museR, 2026-10-13 (post V1-accepted + V2-accepted)

## Rulings this plan rests on
- **V1 ACCEPTED** (cycle 24): 78/78 completable DONE, T-036 record PASS, crash re-run 8/8.
- **V2 ACCEPTED** (this plan): 10/10 DONE, all verified+committed. Evidence: hermetic Q9 re-pass
  0.9888/1.0000 + abstention 1.0000 (transcripted); live leg hits@5 6/7 with anti-overcite
  caveats (102-authentic subset, debris corpus disclosed); G5 recall 0.990 + grounded 0.950
  on scratch; paired eval delta 0.0 complete-pair (honest null, no gate claim). Caveats carried:
  debris corpus, mixed-identity collection live, hermetic limits. Same bar as V1.
- **Dev21 stream ADOPTED, not reverted** (`0d23253`): its B-R1-1..4 diff verified finding-by-
  finding against REW2-V4-B-R1.md (212 green, ruff+mypy clean) and committed as T-V4-001..004
  with provenance on record. Rationale: the tickets asked for exactly what the tree held;
  destroying verified value to re-derive it would be waste dressed as discipline.
- **Standing constraints into V4:** embedder = local bge-m3 (no ollama exists); LLM = OpenRouter
  FREE-only (paid → user approval, never substituted); loop protocols unchanged (one slice,
  allowlisted, push-on-done, verify+commit per delivery).

## Lanes (disjoint by directory + ticket line)
- **Lane A — dev1, backend/workflow core:** B-slice/R2 line (005) → validator/compiler/executor
  chain (006–013) → W6/W7 tail (024–027). Home dirs: `packages/workflow_engine/` (non-adapter
  core), `packages/agents/adapters/` only if a V4 ticket names it.
- **Lane B — dev2, W-line + UI finish:** T-V3-024 (in flight, research UI) → W3 V3-backed executor
  (015–016) → W4 API/contracts (017–020) → W5 editor/view-models (022–023) → T-V4-014 V3 seam.
  Home dirs: `apps/web/`, `packages/agents/adapters/` (W3 only), API contract files.
- **Collision rule:** `packages/workflow_engine/adapters/langflow_adapter.py` is Lane A only;
  `packages/graph/` + `packages/answer/` are CLOSED unless a V4 ticket names them (they belong
  to accepted V1/V2 — any touch needs a new ticket, not a drive-by). Shared files
  (`tests/unit/test_workflow_langflow_adapter.py`) = disjoint hunks or BLOCKED push.
- **T-V4-021 (B-1 production-execution gate — THE gate):** coordinator-dispatched late, after
  006–013 + 015–020 land. Not dispatchable until the validator/executor surface exists.

## Order
1. T-V4-005 (dev1, NOW): determine whether the R2 vehicle exists in the adopted diff or must be
   built; either way end with the enforcement point proven (config-validation rejection test).
2. T-V3-024 (dev2, in flight): close V3 27/27 on landing.
3. Then dev2 → 015 → 016 → 017 → 018 → 019 → 020 → 014 → 022 → 023 (W-line in card order).
4. Dev1 → 006 → 007 → 008 → 009 → 010 → 011 → 012 → 013 (validator/compiler/executor in order).
5. Tails (024–027) + T-021 gate dispatch when both lanes converge.
6. V3 UI re-review (T-023 lane-break) rides dev2's return to frontend-adjacent work.

## Board state at plan time
V1 78 DONE + 1 CANCELLED · V2 10/10 DONE · V3 26/27 (024 IN-PROGRESS) · V4 001–004 DONE
(adopted), rest QUEUED after this plan executes.
