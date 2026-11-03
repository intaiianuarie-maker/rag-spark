# Remediation Report

**Project:** RAG Spark Portable — Dashboard Rust  
**Working Directory:** `C:\Users\ciprian.stoica\_DGX SPARK\Dashboard rust`  
**Date:** 2026-11-03  
**Status:** All identified gaps implemented; pending final test execution and commit.

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Files modified | 7 |
| Gaps addressed | A1, A2, A5, A6, B16 |
| New unit tests added | 5 |
| Syntax checks passed | 7 / 7 |
| Tests executed | Not yet (pytest-asyncio not installed) |
| Commits | None yet (all changes untracked) |

---

## Changes by Gap

### A1 — OpenRouter Retry/Fallback Policy, Refusal Detection, Payload Fields

**File:** `packages/models/adapters/openrouter_provider.py`

| Change | Detail |
|--------|--------|
| Client errors are terminal | Added `_is_client_error(status_code)` returning `True` for 4xx (400–499). These are never retried and never escalated to paid fallback. |
| Refusal detection | Added `_is_refusal(resp_text)` matching refusal markers (refusal, refused, "i cannot fulfill", "violates", "policy violation", etc.). Refusals raise `LLMProviderError` immediately. |
| Payload fields | `generate()` and `stream()` now include `provider.only` (from `request.metadata["provider_only"]`) and `data_collection` (default `"off"`). |
| `generate()` behavior | Client errors + refusals raise immediately — no retry, no fallback. |
| `stream()` behavior | Same client-error/refusal handling; payload fields added. |

**Test file:** `tests/unit/test_openrouter_provider.py` — 5 new tests:
1. `test_client_error_400_not_retried_no_fallback` — 400 → exactly 1 call, 0 fallbacks
2. `test_client_error_401_not_retried_no_fallback` — 401 → exactly 1 call, 0 fallbacks
3. `test_client_error_403_not_retried_no_fallback` — 403 → exactly 1 call, 0 fallbacks
4. `test_refusal_not_retried_no_fallback` — refusal body → exactly 1 call, 0 fallbacks
5. `test_payload_carries_provider_only_and_data_collection` — asserts `provider.only` and `data_collection` in payload

---

### A2 — Workspace Config Reads from DB Instead of `os.environ`

**File:** `packages/models/router_config.py`

| Change | Detail |
|--------|--------|
| Shared entry parser | Added `_entry_to_config()` to parse a config entry (DB row or env var string) into `WorkspaceModelConfig`. |
| `load_workspace_config()` | Now accepts optional `session` (SQLAlchemy async session). When provided, queries `model_configs` table first; falls back to `WORKSPACE_MODEL_CONFIG` env var when DB is unavailable or no row exists. |
| Backward compatibility | Callers without a session continue to use the env var path unchanged. |

---

### A5 — UNRESPONSIVE Verdict, Scrubbed Exceptions, Stage 11 Abstain

**File:** `packages/answer/semantic_verification.py`

| Change | Detail |
|--------|--------|
| New verdict | Added `Verdict.UNRESPONSIVE = "unresponsive"` to the `Verdict` enum. |
| New reason code | Added `REASON_VERIFIER_UNRESPONSIVE = "VERIFIER_UNRESPONSIVE"`. |
| Reason mapping | Updated `_DEFAULT_REASON` mapping: `UNRESPONSIVE → VERIFIER_UNRESPONSIVE`. |
| Export | Added both new symbols to `__all__`. |
| Exception scrubbing | `LIFECYCLE_RECHECK_ERROR` no longer interpolates raw `{exc}` into `answer.reason` — uses a sanitized message to prevent leaking exception text. |
| Decision synthesis | `UNRESPONSIVE` verdict maps to `PublicationStatus.NEEDS_REVIEW` with `can_publish=False` (fail-closed). |

**File:** `packages/answer/engine.py`

| Change | Detail |
|--------|--------|
| Stage 11 exception → abstain | When `validate_citation_integrity()` raises, engine now returns `INSUFFICIENT_EVIDENCE` with `CITATION_INVALID` instead of falling through to publish as "verified". |
| Removed `"verified" if citations else None` fallbacks | Removed from all 4 `pub_status` constructions (2 in `answer()`, 2 in `stream()`). Without gate validation, `pub_status` is now `None` instead of falsely claiming "verified". |
| `verifier_model` alignment (A1 overlap) | Changed from `getattr(self.semantic_verifier, "_model", self.verifier_model)` (the Gemma verifier model) to `self.model` (the serving generation model) in both `answer()` and `stream()`. |


---

### A6 — OR-Capable Full-Text Search Query

**File:** `packages/retrieval/adapters/pg_fts.py`

| Change | Detail |
|--------|--------|
| Query builder | Replaced all 3 occurrences of `plainto_tsquery` with `websearch_to_tsquery`. |
| Semantics | Multi-word terms are now OR'd (any term matches) instead of AND'd (all terms must match), preventing zero-row results for multi-word queries. |

---

### B16 — 404 / 4xx Treated as Unreachable

**File:** `apps/api/app/adapters/readiness.py`

| Change | Detail |
|--------|--------|
| Reachability check | Changed `reachable=response.status_code < 500` to `reachable=200 <= response.status_code < 400`. |
| Effect | 404 (missing health route) and other 4xx responses now correctly report the gateway as unreachable. |

---

## Verification Results

All modified files pass `python -m py_compile`:

| File | Status |
|------|--------|
| `packages/models/adapters/openrouter_provider.py` | ✅ Pass |
| `packages/models/router_config.py` | ✅ Pass |
| `packages/answer/semantic_verification.py` | ✅ Pass |
| `packages/answer/engine.py` | ✅ Pass |
| `packages/retrieval/adapters/pg_fts.py` | ✅ Pass |
| `apps/api/app/adapters/readiness.py` | ✅ Pass |
| `tests/unit/test_openrouter_provider.py` | ✅ Pass |

**Test execution:** Not confirmed. `pytest` / `pytest_asyncio` availability and run results were not returned in this session. The async tests in `test_openrouter_provider.py` require `pytest-asyncio` which is not installed in this environment. They follow existing test patterns and should run when the plugin is available.

---

## Remaining Items

| Item | Status | Notes |
|------|--------|-------|
| Execute new tests in `test_openrouter_provider.py` | Pending | Requires `pytest-asyncio` installation |
| Commit all resolved gaps | Pending | All changes are currently untracked |
| Plan doc untracked | Pending | Remediation plan document not yet committed |

---

## Files Modified (Absolute Paths)

1. `C:\Users\ciprian.stoica\Antigravity\rag-spark-portable\packages\models\adapters\openrouter_provider.py`
2. `C:\Users\ciprian.stoica\Antigravity\rag-spark-portable\packages\models\router_config.py`
3. `C:\Users\ciprian.stoica\Antigravity\rag-spark-portable\packages\answer\semantic_verification.py`
4. `C:\Users\ciprian.stoica\Antigravity\rag-spark-portable\packages\answer\engine.py`
5. `C:\Users\ciprian.stoica\Antigravity\rag-spark-portable\packages\retrieval\adapters\pg_fts.py`
6. `C:\Users\ciprian.stoica\Antigravity\rag-spark-portable\apps\api\app\adapters\readiness.py`
7. `C:\Users\ciprian.stoica\Antigravity\rag-spark-portable\tests\unit\test_openrouter_provider.py`

---

## Next Steps (Recommended Order)

1. Install `pytest-asyncio` and run `tests/unit/test_openrouter_provider.py` to validate the 5 new tests.
2. Fix any test failures that surface.
3. Commit all 7 modified files along with the remediation plan document.
4. (Optional) Review `research.py`/`planner.py` for remaining research-planner gaps flagged earlier.

