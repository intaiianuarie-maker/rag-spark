# T-V1-061a — Q9 hermetic bench RE-RUN — verbatim transcript

**Status: GATE PASS.** Groundedness **0.9888** (>= 0.90), citation validity **1.0000** (== 1.0), exit 0.
**Every one of the 6 rows is bit-identical to the 2026-10-12 baseline. Zero drift.**

This file is the verbatim record museR asked for (T-061 probe FLAG-6: the prior pass had
no logged invocation anywhere in the repo). Nothing here is reconstructed or summarised in
place of the raw output — the raw block below is the terminal output, unedited.

---

## 1. Provenance (closes the FLAG-6 gap)

| item | value |
|---|---|
| run timestamp (UTC) | `2026-10-13T09:54:05Z` |
| repo | `Super Rag/knowledge-os` |
| HEAD at run | `196c3ad4bd7aea1973e16e79bc70636412723803` |
| working tree | dirty — 25 foreign paths (V4/workflow-engine stream + `handoffs/*`), **0 of them mine**; `evals/` untouched |
| index | empty (0 staged) |
| interpreter | `/root/ko-venv/bin/python3` → Python 3.14.4 |
| **command** | `/root/ko-venv/bin/python3 evals/q9_run_benchmark.py` (cwd: repo root) |
| harness sha256 | `f8631cf56ad2851363166f41204714345e07cab923d31277935f00860f93c98d` (`evals/q9_run_benchmark.py`) |
| **slices sha256** | `5004ddc2fa3d86a1ff2a7bbcbb29e9e8e65f2e39910d4c8daacd8da8a8b7ddff` (`evals/q8_eval_slices.jsonl`) |
| slices mtime | `2026-10-12 11:29:39 +0200` — **frozen snapshot, not rebuilt** (per instruction) |
| slices content | 50 records — en/ro/pt/tables/multi x 10 |
| services needed | none — hermetic (no Qdrant, no Infinity, no worker, no DB, no model, no env vars) |
| execution | **attached in the foreground shell**, never detached |

## 2. Raw output (verbatim, unedited)

```
=== PRE-RUN provenance ===
2026-10-13T09:54:05Z
HEAD: 196c3ad4bd7aea1973e16e79bc70636412723803
slices sha256: 5004ddc2fa3d86a1ff2a7bbcbb29e9e8e65f2e39910d4c8daacd8da8a8b7ddff
slices mtime: 2026-10-12 11:29:39.008831977 +0200
harness sha256: f8631cf56ad2851363166f41204714345e07cab923d31277935f00860f93c98d

=== RUN (attached, foreground) ===
Q9 benchmark — real multilingual slices (gold-context oracle)
slice        n groundedness  cite_validity  failures
en          10       1.0000         1.0000  -
ro          10       1.0000         1.0000  -
pt          10       0.9853         1.0000  -
tables      10       1.0000         1.0000  -
multi       10       0.9586         1.0000  -
OVERALL     50       0.9888         1.0000  -

GATE: PASS — groundedness 0.9888 (>= 0.9), citation_validity 1.0000 (== 1.0)
BENCH_EXIT=0
```

Process exit status: **0**.

## 3. Per-slice vs baseline

Baseline source: `handoffs/CLINE1-V1-Q8-Q9-R1.md` lines 60–71 (the 2026-10-12 pass).

| slice | n | groundedness (baseline) | groundedness (re-run) | Δ | cite_validity (baseline) | cite_validity (re-run) | Δ |
|---|---|---|---|---|---|---|---|
| en | 10 | 1.0000 | 1.0000 | **0** | 1.0000 | 1.0000 | **0** |
| ro | 10 | 1.0000 | 1.0000 | **0** | 1.0000 | 1.0000 | **0** |
| pt | 10 | 0.9853 | 0.9853 | **0** | 1.0000 | 1.0000 | **0** |
| tables | 10 | 1.0000 | 1.0000 | **0** | 1.0000 | 1.0000 | **0** |
| multi | 10 | 0.9586 | 0.9586 | **0** | 1.0000 | 1.0000 | **0** |
| **OVERALL** | **50** | **0.9888** | **0.9888** | **0** | **1.0000** | **1.0000** | **0** |

Per-slice failure ids reported by the harness: **none on any slice** (`failures` column is `-` throughout).

Gate thresholds: groundedness >= 0.90 → **0.9888 PASS**; citation validity == 1.0 → **1.0000 PASS**.

## 4. What this run DOES and DOES NOT evidence — read before citing

Measured, and safe to cite:

* **`answer.verification.verify_claims` + `ExtractiveGrounder`** — the answer-layer verification
  and disclosure primitives — are green on all 50 pinned records, with the fabricated
  `dead-fabricated-chunk` citation asserted on every record and surviving zero times.
* `app.eval.metrics.score_faithfulness` unchanged on this corpus.
* No regression introduced by work landing **after** the baseline: `packages/answer/verification.py`
  was last changed by `8273fdb` (T-V1-024) on 2026-10-13, i.e. **after** the 2026-10-12 baseline,
  and these metrics did not move. That is the genuine re-evidencing this re-run buys.

NOT measured — must never be cited from this run:

* **Embeddings / bge-m3 / the Qdrant collection.** Zero retrieval dependency; gold context is
  supplied by the harness. This run says nothing about the live retrieval leg (that is T-061b).
* **`packages/answer/engine.py` — and therefore T-V1-052's all-paths `_finalize` disclosure
  rewrite.** The harness imports `answer.models` and `answer.verification` only; it never imports
  `answer.engine` and never calls `answer()`, and it invokes `verify_claims` directly with
  in-memory `items` and `chunk_lookup=None`. `answer/verification.py` imports `.hydration`, never
  `.engine`.

  Empirically confirmed rather than argued: `packages/answer/engine.py` has been modified **5
  times** since the baseline (`f2e29cd` T-026, `7ce9711` T-031, `0d3e0ec` T-031b, `01e1add`
  T-051, `2a6a806` T-052) and these six metrics moved by **exactly zero**. A harness that
  exercised the rewritten return paths could not be that insensitive.

  **The ticket's stated premise — that the re-run "genuinely re-evidences" T-052 because the
  baseline predates its all-paths rewrite — does not hold for this harness.** The correct gate
  for T-052 remains `tests/unit/test_answer_lost_region_disclosure.py`.

## 5. Reproduction

```bash
cd "/root/code/Super Rag/knowledge-os"
git rev-parse HEAD                      # expect 196c3ad4bd7aea1973e16e79bc70636412723803
sha256sum evals/q8_eval_slices.jsonl    # expect 5004ddc2fa3d86a1ff2a7bbcbb29e9e8e65f2e39910d4c8daacd8da8a8b7ddff
/root/ko-venv/bin/python3 evals/q9_run_benchmark.py
```

Expected: the section-2 output verbatim and exit 0.

Do **not** run `evals/run_q9_benchmark.py` (tautological citation check), and do **not** rebuild
`evals/q8_eval_slices.jsonl` (would break the apples-to-apples comparison against this baseline).