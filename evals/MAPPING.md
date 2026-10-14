# Eval metric mapping — ours vs official (T-EVAL-007)

Every metric our harness can emit is either **official** (produced by vendored
upstream code under `evals/vendor/`) or **ours-only** (produced by our own
primitives). Nothing is blended across the line: the harness's tables carry
two structurally separate column groups, and a test asserts the separation
(`assert_columns_separate`).

## Official (vendored upstream code)

| Metric | Dataset | Vendored source |
|---|---|---|
| `answer_em` / `answer_f1` | HotpotQA | `evals/vendor/hotpot_evaluate_v1.py` |
| `supporting_fact_em`, `supporting_fact_precision`, `supporting_fact_recall`, `supporting_fact_f1` | HotpotQA | `evals/vendor/hotpot_evaluate_v1.py` |
| `joint_em`, `joint_precision`, `joint_recall`, `joint_f1` | HotpotQA | `evals/vendor/hotpot_evaluate_v1.py` |
| `squad_exact_match` / `squad_f1` | XQuAD (SQuAD-F1) | `evals/vendor/squad_evaluate_v2.py` |
| `twowiki_answer_em` / `twowiki_answer_f1` | 2WikiMultihopQA | **placeholder** — official script pending |
| `musique_answer_em` / `musique_answer_f1` | MuSiQue | **placeholder** — official script pending |
| `bfcl_ast_accuracy` | BFCL | **placeholder** — official script pending |

## Ours-only (never blended into official columns)

| Metric | Ours source | Why it is ours-only |
|---|---|---|
| `retrieval_recall` | `app/eval/metrics.py::compute_recall` | retrieval metric; no official counterpart on these QA boards |
| `retrieval_precision` | `app/eval/metrics.py::compute_precision` | as above |
| `retrieval_mrr` | `app/eval/metrics.py::compute_mrr` | as above |
| `retrieval_ndcg` | `app/eval/metrics.py::compute_ndcg` | as above |
| `citation_accuracy` | `app/eval/metrics.py::compute_citation_accuracy` | citation-fabrication metric; product-specific |
| `groundedness_lexical_proxy` | `app/eval/metrics.py::score_faithfulness` (default scorer) | **deterministic lexical proxy** — OURS-ONLY, never blended into an official faithfulness/F1 column; the injectable scorer seam allows RAGAS or an LLM judge without touching the columns |

## Why the separation is structural

`evals/leaderboard.py` keeps `official` and `ours` cell dicts on the row and
`assert_columns_separate` refuses any name that appears in both. A placeholder
official metric renders as `pending`, never as a value, so a reader cannot
mistake an unrun official metric for a measured one.

## Precision

Headline official metrics render with the public-leaderboard precision
(`LEADERBOARD_PRECISION = 1` decimal, 0–100 scale), matching HotpotQA/SQuAD
leaderboards.
