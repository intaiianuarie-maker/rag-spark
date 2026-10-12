# CLINE1 — V1-Q8-Q9-BENCHMARK R1 (submitted for review)

Scope: `V1-Q8-Q9-BENCHMARK.md` only (prerequisites Q1/Q3 approved). No commit,
merge, deploy, or gate closure claimed. Coordinator owns acceptance.

## Changed files (allowed only)

- `evals/build_q8_slices.py` — Q8 pinned-slice manifest builder. Reads the REAL
  datasets under `/root/code/Super Rag/datasets/rag-eval/` and emits
  deterministic, content-hashed JSONL records (10 per surface):
  - `en` — XQuAD `xquad.en` validation (extractive QA)
  - `ro` — XQuAD `xquad.ro` validation (Romanian, same paragraphs)
  - `pt` — PIRA `test.parquet` PT abstracts (`question_pt_origin`)
  - `tables` — T2 FinQA/TAT-DQA `metadata.jsonl` (table-flavoured context)
  - `multi` — RAGBench `hotpotqa` (multi-document `documents`)
  Each record pins `slice, source_id, source_path, question, context,
  gold_answer, content_sha256`. Added `load_slices()` (read-back seam for the
  Q9 runner/tests).
- `evals/q8_eval_slices.jsonl` — 50 pinned records (10/slice), byte-reproducible
  (SHA `5004ddc2…` across rebuilds; fixed per-slice seeds).
- `evals/q9_run_benchmark.py` — Q9 benchmark runner. Hermetic: gold context is
  the retrieval oracle (in-memory chunks); no Qdrant/Infinity/gateway.
  Two independent gates:
  - **groundedness** = `app.eval.metrics.score_faithfulness` (the harness's
    defined RAGAS-swappable scorer) between the asserted claim (normalized gold
    answer) and the gold context.
  - **citation validity** = the REAL product path `answer.verification.verify_claims`
    with the default `ExtractiveGrounder`: a fabricated/dead citation is injected
    on every record and must be dropped. Validity = records with all surviving
    citations real/in-scope/grounded AND no fabricated id surviving / total.
- `tests/integration/test_benchmark_gate.py` — gate test: pinned slices cover all
  surfaces; overall gate (groundedness ≥ 0.90, citation validity == 1.0, zero
  fabricated survived); per-slice gate. Skips loudly if slices missing.

Diff stat (allowed files): 3 new + 1 modified. `apps/api/app/eval/` unchanged
(harness reused as-is).

## Honesty / measurement notes

- Groundedness uses the harness's own `score_faithfulness` (lexical proxy) — the
  metric the eval harness defines and that RAGAS would replace at runtime. It
  measures "is the asserted answer grounded in its retrieved context".
- Citation validity exercises the REAL product verification gate, not a reimpl:
  `verify_claims` + `ExtractiveGrounder`. The dead/fabricated citation
  (`dead-fabricated-chunk`) is asserted on all 50 records and survives 0 times.
- The claim text is the normalized gold answer (list-literal answers like
  `"['47,880']"` unwrapped to `47,880`); no wording is invented.
- This is a **gold-context oracle** run: it proves groundedness + citation
  validity when the correct context is retrieved. It does NOT measure retrieval
  recall/MRR (that needs the live Qdrant/FTS index and is reported separately).

## Command outputs (from repo root; Python 3.14.4)

1. `/root/ko-venv/bin/python3 -m py_compile evals/*.py` → OK
2. `/root/ko-venv/bin/pytest tests/unit/test_eval.py -q` → **12 passed in 0.21s**
3. `/root/ko-venv/bin/ruff check evals/ apps/api/app/eval/` → **All checks passed!**
   (2 interim findings fixed: I001 import order, RUF005 list unpack)
4. `/root/ko-venv/bin/python3 -m mypy apps/api/app/eval` → **no issues in 6 files**

Benchmark metric results (`evals/q9_run_benchmark.py`):

| slice  | n | groundedness | cite_validity |
|--------|---|--------------|---------------|
| en     | 10 | 1.0000 | 1.0000 |
| ro     | 10 | 1.0000 | 1.0000 |
| pt     | 10 | 0.9853 | 1.0000 |
| tables | 10 | 1.0000 | 1.0000 |
| multi  | 10 | 0.9586 | 1.0000 |
| OVERALL| 50 | **0.9888** | **1.0000** |

`GATE: PASS — groundedness 0.9888 (>= 0.9), citation_validity 1.0000 (== 1.0)`

Gate test `pytest tests/integration/test_benchmark_gate.py -q` → **3 passed**.

## Remaining limits (not claimed)

Q8/Q9 product acceptance, retrieval-recall benchmark on the live index, merge,
deployment remain open. `pyarrow` was installed into the venv to read the real
parquet datasets (no repo dependency change). Other streams' edits untouched; no
commit performed.
