# CLINE1 — V1-Q1-FINITE-EMBEDDINGS R1 (submitted for review)

Scope: `V1-Q1-FINITE-EMBEDDINGS.md` only (prerequisite: scoped zero-vector fix
approved in `reviews/V1-Q1-REVIEW-1.md`). No commit, merge, deploy, or gate
closure claimed. Coordinator owns acceptance.

## Changed files (allowed only)

- `packages/retrieval/adapters/infinity_embed.py`
  - SHA-256: `888236fe74c9fbae9965ec5acdb33bdbf3ff027a70e9344fc324d064c339a79f`
  - Component conversion wrapped in narrow `except (TypeError, ValueError,
    OverflowError)` → `EmbeddingProviderError("... invalid numeric component")`,
    chained (`from exc`); no raw component contents in the message.
  - `math.isfinite` rejection of every converted component **before** dimension
    adaptation (NaN, ±Inf, and convertible strings such as `"nan"` / `"inf"` /
    `"-inf"`); message `"... non-finite embedding component"`.
  - Zero/near-zero check kept after dimension handling, threshold unchanged
    (1e-9), implementation switched to `math.hypot(*vector)` so finite large
    values cannot raise a square/sum overflow (hypot returns `inf` → not
    below threshold → accepted).
  - Whole-call failure preserved: any invalid vector raises; no substitution,
    skip, partial return, retry, or fallback.
- `tests/contracts/test_infinity_embedding_contract.py`
  - SHA-256: `fec774eb9f7ba7d0a0a1cd7e9ec3439fa948225f31a25c3fe22adf5f15b7313f`
  - Small parameterized fake-server mode `invalid_component:<case>` with the
    invalid component in the **final** vector after a valid vector; cases:
    `nan`, `pos_inf`, `neg_inf`, `str_nan`, `str_inf`, `str_neg_inf`,
    `non_numeric`, `null`. Each asserts `EmbeddingProviderError` for both
    `embed_query` and `embed_documents(["alpha", "beta"])` (≥2 items).
  - `nonfinite_truncated_tail`: NaN in a 64-component tail past 1024-d that
    adaptation would truncate — still rejected; env isolated via
    `pytest monkeypatch.setenv("ALLOW_DIMENSION_ADAPTATION", "true")`.
  - `near_zero`: 1024 × 1e-12 rejected with `zero-vector` for both entry points.
  - Finite passthrough: deterministic vectors survive unchanged.
  - `large_finite`: 1024 × 1e308 returns without arithmetic overflow.
  - Existing zero-vector / mixed-batch tests kept.

Diff stat: 2 files, 247 insertions, 4 deletions (per `git diff --stat`).

## Before / after behavior

- Before: `float(value)` accepted JSON-string `"nan"`/`"inf"` (converted to
  NaN/Inf) and bare NaN/Inf floats; non-numeric/`null` raised untyped
  `TypeError`/`ValueError`; norm used `sqrt(fsum(v*v))` which can raise
  `OverflowError` on uniform 1e308 vectors (verified: `math.fsum` path raises;
  `math.hypot(*([1e308]*1024))` returns `inf`).
- After: all 8 invalid-component cases raise typed `EmbeddingProviderError`
  (verified via fake HTTP server for both entry points, plus MockTransport for
  the JSON-serializable string/null subset; bare NaN/Inf cannot round-trip
  through `httpx.MockTransport`'s strict `allow_nan=False` encoder, so they are
  covered by the fake-server bytes path and direct `math.isfinite` checks).
  Truncated-tail NaN still rejected. 1024 × 1e-12 rejected as zero-vector.
  1024 × 1e308 returns intact. Valid vectors byte-identical.

## Verification (exact spec order, from repo root)

Interpreter: Python 3.14.4 (`/root/ko-venv/bin/python3 --version`).

1. `/root/ko-venv/bin/python3 -m py_compile
   packages/retrieval/adapters/infinity_embed.py
   tests/contracts/test_infinity_embedding_contract.py` → `PY_COMPILE_OK`
2. `/root/ko-venv/bin/python3 -m pytest
   tests/contracts/test_infinity_embedding_contract.py -q`
   → `29 passed in 16.05s` (was 17 pre-change; +12: 8 invalid-component +
   truncated-tail + near-zero + finite-passthrough + large-finite)
3. `/root/ko-venv/bin/ruff check
   packages/retrieval/adapters/infinity_embed.py
   tests/contracts/test_infinity_embedding_contract.py`
   → `All checks passed!` (one interim RUF043 on a non-raw `match=` alternation
   fixed with `r"..."`)
4. `/root/ko-venv/bin/python3 -m mypy packages/retrieval`
   → `Success: no issues found in 9 source files`

Independent probes: MockTransport string/null subset (`"nan"`, `"inf"`,
`"-inf"` → non-finite error; `"abc"`, `None` → invalid-numeric error, both
entry points); direct checks that `math.hypot` large-finite → `inf` (accepted),
near-zero → below 1e-9 (rejected), and `isfinite` precedes
`ALLOW_DIMENSION_ADAPTATION` in `_embed` source.

## Remaining limits (not claimed)

Q1 product acceptance, live ingestion/store lifecycle, Q8/Q9, V2 G4/G5, merge,
and deployment remain open. Other streams' edits untouched; no graph/answer/core,
contract helpers, config, deps, migrations, callers, or other tests modified.
No commit performed (working-tree modifications to the two allowed files only).
