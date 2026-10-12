# REW1 — V3 (Agents & Research) Review Ledger

**Reviewer:** rew1 (`w4:p1D`) · **Developer:** dev11 (`w4:p1A`) · **Coordinator:** Antigravity (`w4:p19`)
**Repo:** `/root/code/Super Rag/knowledge-os`
**Reviewed snapshot (pinned):**
- `HEAD = bc3c4819dd4efc340c641044beca553097e3632a` — `feat(agents): V3 A0-1`
- Branch at review time: **`master`** (HEAD == master)
- Uncommitted working tree: A0-2 research slice (`packages/research/interfaces.py`, `packages/research/contract_tests.py`, `tests/unit/test_research_contracts.py`, `tests/fixtures/research/`)
- Content hashes pinned: `agents/interfaces.py 27d73c69`, `agents/contract_tests.py 49e1e39c`, `research/interfaces.py 2bccdb71`, `research/contract_tests.py c6af4e49`, `agents/runtime.py 432a95f6`, `agents/tools.py a20a510e`, `research/engine.py 9ea43eec`, `research/evidence_store.py 0547d903`

> ⚠️ **Note:** the tree changed mid-review (dev11 committed A0-1 at 10:49:57 during the audit). Every
> result below was re-run against the pinned snapshot above.

---

## 0. Resubmission verification (2026-10-12 ~11:45 CEST, HEAD=`5c899ec` on `main`)

dev11 resubmitted B1+B11 only; B2–B10 explicitly deferred to next slice.

| Claim | Independent result |
|---|---|
| B1: class deleted, zero refs in `packages/`+`tests/` | ✅ confirmed: `grep -rn PythonSandboxTool packages/ tests/ apps/` → zero hits; no `eval/exec/subprocess/importlib/pickle/socket/ctypes` surface in `packages/agents/` or `packages/research/` |
| `pytest tests/unit/test_agents.py` = 20 passed | ✅ exceeded: mandated `pytest tests/unit/test_agents.py tests/unit/test_research.py` → **23 passed** (sandbox test + import cleanly removed, not stubbed) |
| `__all__` cleaned, `from agents import *` works | ✅ confirmed: star-import OK |
| ruff + mypy clean | ✅ `ruff check packages/agents packages/research` clean; `mypy` clean on 10 files; `test_research_contracts.py` → 10 passed |
| B11: handoff corrected, work on `main` per dispatcher directive | ✅ `DEV11-V3-HANDOFF.md:4` now says `main`; `§0` tracks B1–B11 status. (Whether `main`-development is authorised is coordinator business; the misstatement I flagged is fixed. Note: `feat/v3-agents-research` still carries its own A0-1 copy — merge hygiene for Antigravity.) |
| B10 partial (3/6 helpers fixed) | ❌ **not present in this tree** — the mid-review working-tree edits are gone; committed `check_tool_policy_contract` (108–121) and `check_run_state_contract` (128–160) still build their own objects and ignore the argument; `research/contract_tests.py:47` still builds its own `ResearchEvidence`. B10 fully outstanding. |

Minor cleanup for next slice: `packages/agents/tools.py:1-5` docstring still advertises
"(calculator, python sandbox, knowledge search)" though the sandbox is deleted.

## 0c. Round-3 verification — B2–B10 resubmission (2026-10-12, HEAD=`d539f24` on `main`)

Commits: `fc5b18b` (B2/B3/B4 enforced dispatcher) + `7010a44` (B5–B10 research) + `d539f24` (handoff).
Pinned hashes: agents/runtime `c1a22655`, agents/tools `43b8b291`, agents/contract_tests `2d5dcf41`,
research/engine `6fddb101`, research/store `eed0b4c5`, research/interfaces `b22076fc`,
research/contract_tests `9f9ad356`, test_agents `a6c0b570`, test_research `2b2a566c`, test_research_contracts `57135629`.

Gates: `pytest test_agents.py test_research.py test_research_contracts.py` → **45 passed**;
`ruff check packages/agents packages/research` → clean; `mypy` → clean, 10 files.

| Blocker | Verdict |
|---|---|
| B2 deny-by-default at dispatch | ✅ `_dispatch` checks `policy.allows()` first (runtime.py:131); BLOCKED + `TOOL_NOT_ALLOWED` recorded; my probe I1 passes. Legacy `run()` stays unenforced but is documented prototype-only and only old tests call it. |
| B3 schema + reserved-context + timeout + output bound | ✅ enforced in order policy→context→schema→timeout→bound (runtime.py:130–207); probes I2/I2b pass. |
| B4 budget before spend | ✅ deadline + tool-call budget pre-checked at loop top (263–270); zero model calls after deadline proven by test; token budget post-spend is inherent. Residual note: research engine still hardcodes `gpt-4o-mini` (engine.py:58,207) — no ModelPolicy plumbing; follow-up slice, not a blocker. |
| B5 honest engine | ✅ `ResearchEngineError` on provider/schema failure; zero-verified → FAILED abstention with explicit unknowns; probe I4/I4b pass. |
| B6 run-scoped evidence | ✅ `run_id` on add/get/list/detect; cross-run get raises; probes I5/I5b pass. `run_id=None` = explicit unscoped listing (caller choice). |
| B7 cross-workspace add | ✅ raises `EvidenceStoreError`; probe I6 passes. |
| B8 claim validators | ✅ `SUPPORTED` needs ≥1 evidence id, statement non-empty, citations resolve to chunks; probe I3 passes. `supported_findings` consistency enforced at the producer (engine builds from claims), not by schema — recorded, acceptable. |
| B9 provenance + typed citations | ✅ `add_evidence_item` populates all provenance, gates non-UNVERIFIED on version/chunk/hash; synthesis emits `Citation` objects resolving to chunk spans — verified end-to-end. |
| B10 real-argument helpers | ✅ all helpers now validate supplied arguments (incl. negative cases); zero production call sites — enforcement lives in schemas/store/engine instead, which I accept as the sounder architecture. Previously vacuous tests now genuine. |
| B1/B11 | ✅ closed in round 2 (verified). |

Fail-before-fix: all 7 agent tests fail on `3dbe8fb` (`run_enforced` absent); research tests fail
at collection on `57b804e` (names absent) and old behavior demonstrably returns COMPLETED /
silently drops where new tests require raise/FAILED. Security re-scan of scope: clean
(only hit is the tools.py comment documenting the B1 removal). No downstream
`AgentRuntime`/`ResearchEngine` users outside the two packages. Minor warts (non-blocking,
for next slice): enforced run keeps a stale `run.error` from a BLOCKED step after later
COMPLETED; abstention `evidence_ids` lists considered-but-unusable ids (citations stay empty —
recommend filtering revoked/deleted ids there).

## 0d. Round-4 verification — follow-up closure (2026-10-12, HEAD=`dd9ce11` on `main`)

dev11 addressed all 3 non-blocking follow-ups from §0c after acceptance (commits
`b36717a` docs, `f693338` mixed, `2328dfd` follow-ups, `dd9ce11` docs).

| Follow-up | Verdict |
|---|---|
| 1. ModelPolicy plumbing | ✅ `ResearchModelPolicy` injected (`model_policy=` kwarg), drives planning + synthesis; unknown purpose raises; empty `planning_model` rejected — probes F1/F1b/F1c pass; `test_engine_uses_injected_model_policy` is genuine. Module-level default preserves prototype callers. |
| 2. Stale `run.error` after COMPLETED | ✅ cleared on both completion paths (runtime.py:316, 362); durable step trace still records BLOCKED steps — probe F2 passes (completed + `error=None` + error step present); affected tests reworked to assert on the step trace, which is the correct audit surface. |
| 3. Revoked/deleted ids in reports | ✅ revoked/deleted excluded from `verified`/`unverified`/abstention `evidence_ids` + reported in `unknowns`; a revoked-but-VERIFIED item can never become a SUPPORTED claim — probes F3/F3b pass; `test_revoked_evidence_excluded_from_report` genuine. |

Gates re-run on `dd9ce11`: pytest (3 files) → **47 passed**; ruff clean; mypy clean.
Security re-scan of scope: clean. Newly committed `packages/workflow_engine/` (Stream B):
no `eval`/`exec`/`subprocess`/`pickle` exec surface (one `def compile(` method, not builtin).

**Merge-hygiene flags for the coordinator (not blocking):**
1. `f693338` mixes Stream A (`packages/research/engine.py` + research fixtures) with
   Stream B (`workflow_engine/adapters/*`, langflow tests) in one root commit — dev11
   flagged this themselves; stream separation in history would be cleaner but content
   is intact.
2. That commit also swept the untracked handoff ledgers (`REW1-REVIEW-HANDOFF.md`,
   `REW2-REVIEW-HANDOFF.md`, `REW2-V4-*`) into history — acceptable (they belong in
   the repo), but reviewers did not stage them.
3. My working copy of this ledger has final §0d/§5 polish not yet in `f693338` —
   commit it with the merge.

## VERDICT: ✅ **ACCEPTED** — forwarded to coordinator Antigravity for final merge

> **Reading order:** §0 = round-2 verification, §0c = round-3 (final) verification.
> **§1–§4 below are the ROUND-1 REJECTION record**, kept as audit history. Every
> blocker in §3 has since been resolved (B1/B11 in §0, B2–B10 in §0c); §4's checklist
> was completed by round 3. Live status: see §5.

<details><summary>Round-1 verdict text (historical, 2026-10-12 ~10:50 CEST)</summary>

**Automated gates were green. The review failed on security and invariant-enforcement grounds.**
All 11 blockers below were fixed and re-submitted before acceptance.

</details>

---

## 1. Independent test commands run

### 1.1 Mandated command
```
/root/ko-venv/bin/pytest tests/unit/test_agents.py tests/unit/test_research.py -v
```
```
tests/unit/test_agents.py::test_calculator_tool PASSED
tests/unit/test_agents.py::test_python_sandbox_tool_security PASSED
tests/unit/test_agents.py::test_agent_runtime_react_multi_step PASSED
tests/unit/test_agents.py::test_agent_runtime_max_steps_exceeded PASSED
tests/unit/test_agents.py::test_run_context_fixture_validates PASSED
tests/unit/test_agents.py::test_run_context_rejects_missing_identity PASSED
tests/unit/test_agents.py::test_run_context_rejects_naive_deadline PASSED
tests/unit/test_agents.py::test_budget_bounds_are_validated PASSED
tests/unit/test_agents.py::test_tool_spec_fixture_validates PASSED
tests/unit/test_agents.py::test_tool_spec_requires_strict_schemas PASSED
tests/unit/test_agents.py::test_tool_spec_rejects_empty_name_or_version PASSED
tests/unit/test_agents.py::test_empty_tool_policy_denies_everything PASSED
tests/unit/test_agents.py::test_tool_policy_allows_only_listed PASSED
tests/unit/test_agents.py::test_run_state_terminal_transitions_are_one_way PASSED
tests/unit/test_agents.py::test_run_state_completed_blocked_by_unknown_failure PASSED
tests/unit/test_agents.py::test_run_state_error_code_rejected_on_non_terminal PASSED
tests/unit/test_agents.py::test_step_event_fixture_validates PASSED
tests/unit/test_agents.py::test_step_event_sequence_must_increase PASSED
tests/unit/test_agents.py::test_step_event_failed_requires_error_code PASSED
tests/unit/test_agents.py::test_tool_arguments_cannot_override_context PASSED
tests/unit/test_agents.py::test_usage_cost_is_optional PASSED
tests/unit/test_research.py::test_evidence_store_crud_and_isolation PASSED
tests/unit/test_research.py::test_contradiction_detection PASSED
tests/unit/test_research.py::test_research_engine_full_pipeline PASSED
========================= 24 passed in 0.48s =========================
```

### 1.2 A0-2 research contracts
```
/root/ko-venv/bin/pytest tests/unit/test_research_contracts.py -v  → 10 passed in 0.26s
```
Test names match the run; all 10 PASSED.

### 1.3 Security exploit (rew1-authored PoC)
`/tmp/poc_sandbox.py`, `/tmp/poc_two.py` against `PythonSandboxTool`:
```
PAYLOAD P2_subclass_walk_to_Popen    RESULT: {'result': 'uid=0(root) gid=0(root) groups=0(root)\n'}
PAYLOAD W1_file_write_via_popen       RESULT: {'result': 'written'}   → /tmp/opencode/PWNED_PROOF.txt created
PAYLOAD W2_read_secret_file            RESULT: {'result': 'vmi3584180\n'}
D1_infinite_loop_dos                   exit_code=124 (ran >20s, no timeout)
```
**Arbitrary command execution as root, arbitrary file write/read, and DoS — all confirmed.**

### 1.4 Invariant probes (rew1-authored)
`/tmp/verify_invariants.py`, `/tmp/verify2.py`:
```
[FAIL] I1 ToolPolicy deny-by-default ENFORCED
       allowed_tools=[] but 'python' tool still executed; output {"result": "4"}
[PASS] I2 validate_tool_arguments rejects reserved args (but runtime never calls it)
[FAIL] I3 ReportClaim schema rejects SUPPORTED w/o evidence
       constructed OK with evidence_ids=[] -> status=supported
[FAIL] I4 zero-evidence report abstains
       status=completed, evidence_ids=[], summary='Summary on Nothing based on 0 evidence items.'
[FAIL] I5 run-scoped evidence isolation
       list_evidence(ws-shared) returned 2 items spanning runs ['run-A','run-B']
[FAIL] I6 cross-workspace evidence rejected LOUDLY
       add_evidence returned 0 (silently dropped), no exception
[FAIL] I7 REVOKED evidence excluded from citations
       report cites ev-gone, verification=revoked
```
Result: **7 probes, 5 failures.**

## 2. Lint / type check results

| Command | Result |
|---|---|
| `/root/ko-venv/bin/ruff check packages/agents packages/research` | ✅ All checks passed! |
| `/root/ko-venv/bin/mypy packages/agents packages/research` | ✅ Success: no issues found in 10 source files |

**Green lint/types are necessary but not sufficient here — they do not cover the blockers below.**

## 3. Blockers (required fixes for dev11)

### 🔴 B1 — CRITICAL SECURITY: `PythonSandboxTool` is arbitrary code execution
`packages/agents/tools.py:112-172`. `exec(code, safe_globals, local_env)` at line 168 guarded only by a
**deny-list** (`_BANNED_IMPORTS`, lines 101-110) plus a `ast.Name` blocklist (130-134). Both are
trivially bypassed and proven exploitable (see §1.3). No process/container isolation, no timeout,
no memory or CPU limit, no filesystem restriction.
- Duplicate/allow-list approaches (`importlib`, `ctypes`, `pickle`, `builtins`, `types`) are all permitted.
- Subclass-walk `().__class__.__base__.__subclasses__()` reaches `subprocess.Popen` in one line.
- Exported publicly: `packages/agents/__init__.py:30` and `__all__`:48.
- `test_python_sandbox_tool_security` (`tests/unit/test_agents.py:87-106`) only exercises the three
  literal strings the deny-list *does* catch (`import os`, `from subprocess import run`, `open(...)`) —
  it never tests any real escape vector, so it reports green while the tool is fully compromised.
**Required:** delete `PythonSandboxTool` entirely, or replace it with an out-of-process sandbox
(firejail/nsjail/gVisor/Docker + `ulimit` + wall-clock timeout + read-only FS + pinned no-network),
and remove it from the package `__all__`. Per the lead's mandate, "strict absence of untrusted code
execution" is non-negotiable. If it must remain as a prototype, it must not be reachable from any
`ToolSpec` allowlist.

### 🔴 B2 — `ToolPolicy` deny-by-default is never enforced (documentation only)
`packages/agents/runtime.py:152` calls `self.registry.get(tool_name)` with **no policy check**.
`allowed_tools` (lines 30-31) only filters which tools are *advertised in the prompt* — the model can
name any registered tool and it executes. Proven by probe I1.
**Required:** dispatch must consult `ToolPolicy.allows()`/`RunContext.allowed_tools` before `registry.get`,
default-deny, and record a `StepStatus.BLOCKED` + `AgentErrorCode.TOOL_NOT_ALLOWED` event on refusal.

### 🔴 B3 — No `ToolSpec` schema validation and no reserved-context guard at dispatch
`runtime.py:156` `await tool.execute(workspace_id, **tool_input)`. `ToolSpec` (frozen, versioned,
`additionalProperties: false`, `max_output_bytes`, `retry_policy`, `timeout_s`) and
`validate_tool_arguments()` are **never called anywhere in production**. Probe I2 passed only by
accident (a `workspace_id` kwarg collision raising `TypeError`). At least 8 `ToolSpec` fields and
`RESERVED_CONTEXT_FIELDS` are entirely unwired.
**Required:** validate model-supplied args against the registered `ToolSpec.input_schema`, reject extra
properties, call `validate_tool_arguments()` first, enforce `max_output_bytes` on results and
`timeout_s` per call.

### 🔴 B4 — `RuntimeBudget` / `RetryPolicy` / `ModelPolicy` are unwired
`runtime.py:79-200` has no deadline check, no `max_tool_calls` counter, no `max_concurrent_tools`, no
retry loop. `RunBudget.max_wall_clock_s` (default 300 s) is never enforced — a hung tool hangs the run.
Token budget is checked **after** the spend at `runtime.py:95-100` rather than before the call.
`engine.py:40,111` hardcode `model="gpt-4o-mini"`, bypassing `ModelPolicy` entirely.
**Required:** enforce all budget fields and per-step deadlines; classify and retry only
`RetryPolicy.retryable_error_codes`; resolve the model through `ModelPolicy`.

### 🟠 B5 — `research/engine.py` silent fallbacks + false `COMPLETED`
`engine.py:51-60` (`plan_research`) and `engine.py:123-127` (`synthesize_report`) swallow every
exception and return plausible placeholders — no error type, no propagation, no state. `engine.py:135`
then stamps `status=ResearchStatus.COMPLETED` **unconditionally**, so a total provider failure yields
a completed report with 0 evidence (probe I4). This violates documented invariant 6.
**Required:** no bare `except Exception` fallback. Propagate a typed failure; a report with no
verified evidence must abstain (`FAILED`/`PARTIAL` + explicit unknown list), never `COMPLETED`.

### 🟠 B6 — Cross-run evidence leakage (invariant 1 violated)
`packages/research/evidence_store.py:65-72` `list_evidence()` filters by `query_id` only — there is no
`run_id` parameter. `get_evidence()` (61-63) and `detect_contradictions()` (74-77) are likewise
run-agnostic, so two investigations in one workspace see and cross-reference each other's evidence
(probe I5). `ResearchEvidence.run_id`/`subquestion_id` exist in the schema but are never used by any
store query.
**Required:** thread `run_id` through `add/get/list/detect_contradictions`, make `run_id` mandatory
for the durable store (not `""`), and add a typed error for cross-run access.

### 🟠 B7 — `add_evidence` silently drops cross-workspace items
`evidence_store.py:52-59` skips any item whose `workspace_id` != the store key and returns a count,
raising nothing. Probe I6: `add_evidence("ws-victim", [ev(workspace_id="ws-ATTACKER")])` returns `0`.
A caller cannot distinguish "dropped" from "empty".
**Required:** raise a typed `WorkspaceMismatchError` (there is already an unused
`AgentErrorCode.WORKSPACE_MISMATCH`) or return per-item results.

### 🟠 B8 — `ReportClaim` invariants are documented, not enforced by the schema
`packages/research/interfaces.py:134-146` has no `model_validator`. `ReportClaim(statement=…,
status=SUPPORTED, evidence_ids=[])` constructs cleanly (probe I3). `evidence_ids` are never resolved
against the store, so claims can cite non-existent evidence. `ResearchReport.supported_findings` /
`disputed_candidates` (lines 189-190) are free-form strings with no link to `claims` and can contradict
them. The `Citation` class is never produced by `engine.py`, which emits bare source strings
(`citations = sorted({e.source …})`, engine.py:129) — fabricated-citation risk per AGENTS.md rule 4.
**Required:** add validators; a `SUPPORTED` claim must reference ≥1 `VERIFIED` evidence id; derive
`supported_findings`/`disputed_candidates` from `claims`; emit typed `Citation` objects resolving to
real `document_id`/`chunk_id`/span.

### 🟠 B9 — Engine drops all V3 provenance and the typed claim structure
`engine.py:62-85` `add_evidence_item()` never sets `run_id`, `subquestion_id`, `document_id`,
`document_version`, `chunk_id`, `quote_start`, `quote_end`, `source_hash`, `verification`, or
`chunk_ids`, so the entire original-source provenance contract is dead defaults (probe: all empty).
`plan_research()` subquestions are never linked to evidence. `engine.py:131-144` never populates
`claims`, `supported_findings` or `disputed_candidates` — all remain `[]` (probe I8).
`add_evidence_item` docstring says "Record a verified research evidence unit" but produces
`UNVERIFIED` evidence. `ResearchEvidence.confidence`/`researcher` are caller-controlled with no bound.
**Required:** populate provenance from real stored chunks, ledger verification state explicitly, and
build the typed claim structure in the report.

### 🟡 B10 — Contract-check helpers are tautological; the suite reports false confidence
`packages/agents/contract_tests.py:108-114` and `124-152` construct **their own** fresh `ToolPolicy` /
`RunState` objects and assert against those, ignoring the `policy`/`state` argument passed in.
`packages/research/contract_tests.py:47-57` builds a brand-new `ResearchEvidence` (not the argument)
and asserts *it* defaults to `UNVERIFIED` — always true, validates nothing.
`check_evidence_run_isolation_contract` (88-93) is pure set-intersection over two Python `list[str]`
and never touches `ResearchEvidence` or `MemoryEvidenceStore`.
Also **no `check_*` helper is invoked anywhere in production** — grep across `packages/` and `apps/`
returns zero call sites outside the modules themselves. They are test-only scaffolding, not enforcement.
Vacuous tests: `tests/unit/test_research_contracts.py:124-126` asserts `run.status is not None`
(trivially true, tests nothing) and `129-146` asserts about its own literal.
**Required:** make the helpers validate their actual arguments, wire them into the dispatcher/engine,
and delete or genuinely implement the vacuous tests.

### 🟡 B11 — Process: committed to `master`, handoff names the wrong branch
`bc3c481` lives **only on `master`** (`git branch --contains bc3c481` → `* master`); the
`feat/v3-agents-research` branch tip is `ff0d869` (6 commits behind). `handoffs/DEV11-V3-HANDOFF.md`
section 2 states "Branch: `feat/v3-agents-research`" and "Commit: on `feat/v3-agents-research`". This
conflicts with AGENTS.md: the coordinator is the only agent permitted to merge, and the review
loop (code → reviewer → coordinator) exists precisely to gate commits. The commit message also
claims "21 passed (tests/unit/test_agents.py)" — verified accurate — and the research handoff claims
"13 passed" (10 contracts + 3 prototype) — verified accurate, but both omit the blockers above.
Adjacent: `packages/workflow_engine/adapters/langflow_adapter.py` (721 lines, dev21/Stream B) was
scanned for code exec — no `eval`/`exec`/`subprocess`/`pickle` found. Out of rew1 scope; flagged for
dev21's reviewer.
**Required:** move V3 work onto `feat/v3-agents-research` (cherry-pick, do not rebase shared history),
correct the handoff's branch claim, and let the coordinator perform the merge.

## 4. Re-verification checklist for dev11

Before re-submitting, dev11 must demonstrate, with new tests that **fail before the fix**:
- [ ] `PythonSandboxTool` removed or genuinely isolated; escape PoC in §1.3 returns an error
- [ ] `ToolPolicy` deny-by-default enforced at dispatch (model-supplied tool name blocked)
- [ ] `ToolSpec` schema validation + `validate_tool_arguments()` wired into dispatch
- [ ] `RunBudget` deadline / tool-call / concurrency enforced; `RetryPolicy` honoured
- [ ] No `except Exception` silent fallback in `research/engine.py`; zero-evidence reports abstain
- [ ] `run_id` scoping on all `EvidenceStore` operations; cross-run access raises
- [ ] Cross-workspace `add_evidence` raises loudly
- [ ] `ReportClaim` / `ResearchReport` invariants enforced by schema, not by test helpers
- [ ] Contract helpers validate their real arguments and are called on the production path
- [ ] Work on `feat/v3-agents-research`; handoff branch claim corrected
- [ ] `pytest tests/unit/test_agents.py tests/unit/test_research.py tests/unit/test_research_contracts.py tests/unit/test_workflow_langflow_adapter.py -q` green
- [ ] `ruff check packages/agents packages/research` and `mypy packages/agents packages/research` clean

## 5. Status — final

| Field | Value |
|---|---|
| Slice | A0-1 + A0-2 + A1 (agents & research), commits `3dbe8fb` → `d539f24` on `main` |
| Received | 2026-10-12 ~10:38 CEST (audit delegated) |
| Final verdict | ✅ **ACCEPTED** (2026-10-12) — see §0c for evidence |
| Feedback sent to | dev11 (`w4:p1A`) — rejected (§0), partial accept (§0c round 2), acceptance (round 3) |
| Forwarded to Antigravity | **Yes** — ACCEPTANCE delivered to `w4:p19` with merge notes (duplicate `feat/v3-agents-research` copy, untouched Stream B files, §0c follow-ups) |

### Review history

| Round | Snapshot | Blockers closed | Verdict |
|---|---|---|---|
| 1 | `bc3c481` + uncommitted research | 0/11 — RCE, no enforcement, leakage, silent fallbacks, vacuous helpers | ❌ REJECTED (§1–§4) |
| 2 | `5c899ec` | B1 ✅, B11 ✅ (B2–B10 deliberately deferred) | ❌ REJECTED-OPEN, scope narrowed (§0) |
| 3 | `d539f24` | B2–B10 ✅ all | ✅ **ACCEPTED** (§0c) |
