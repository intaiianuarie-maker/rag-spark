# HF-BENCH — Paired V1 vs V2 benchmark (bounded, CPU-only)

**Runner:** dev11 · **Date:** 2026-10-12 · **Report to:** museR
**Repo:** `/root/code/Super Rag/knowledge-os` (HEAD `dd9ce11`, branch `main`)
**Datasets:** `/root/code/Super Rag/datasets/rag-eval`
**Status:** ⚠️ **PARTIAL** — retrieval deltas measured on 3 of 4 requested slices,
on a **FAKE runtime** (not production conditions). See honesty contract below.
**No code edits made** (one throwaway data-prep script under `evals/_bench_*`, not product code).

---

## 1. Honesty contract (read before the numbers)

The supplied harness (`tests/fixtures/graph_paired_eval.py`, driven by
`evals/graph_paired_eval.py`) enforces these tags itself and refuses to let them be
conflated. They apply to every number in this report:

- **runtime_mode = `fake` for all rows.** The harness builds `FakeVectorStore`,
  `FakeEmbeddingProvider`, `FakeLexicalStore` and `FakeReranker` (one-hot embeddings,
  token-overlap rerank). `runtime_mode` is *derived from the adapters constructed*,
  is not caller-settable, and stays `fake` regardless of real labels. **These are
  mechanism results, NOT V1 production retrieval conditions.**
- **dataset_origin = `labelled`** for all rows below: every question is real
  (from the pinned Q8 slices, which come from the named corpora). Real labels on a
  fake runtime = mechanism-only, per the harness contract.
- **grounded-answer rate: UNMEASURED.** Neither harness invokes an answer engine.
  The paired harness reports only *oracle gold-answer availability* (is the gold
  answer verbatim in a retrieved chunk) — a retrieval diagnostic that would read
  1.0 even if the system always answered wrongly or abstained. **No grounded-answer
  rate is claimed.** The Q9 arm (below) measures grounding on a gold-context oracle,
  which is a different, weaker claim than end-to-end grounded answers.
- **V1/V2 arms are selected by the `RetrievalMode` argument** (`HYBRID` vs
  `HYBRID_GRAPH`), **not** by the `GRAPH_MODE` env var. Confirmed: the harness never
  reads `GRAPH_MODE`. So "V1 = GRAPH_MODE=off, V2 = on" is realized here as the two
  retrieval modes over one shared corpus, holding corpus + K constant — the correct
  paired comparison, but on fakes.
- **Latency is harness wall-clock on fake adapters**, not production latency. It is
  reported as a relative cost proxy only (graph leg adds extraction + traversal).

## 2. Method

Corpus per slice = the union of every question's gold context, split on blank lines
into chunks, one workspace (`ws-bench`). A question's `relevant_chunk_ids` are only
its OWN gold paragraphs; every other question's gold paragraphs are cross-question
distractors. This keeps the corpus > K so the baseline can genuinely miss (the
failure mode the harness flags). K = 20, context_top_k = 6. Chunk ids derived
deterministically from `(source_id, paragraph index)`. No label or score invented.

## 3. Paired retrieval results (FAKE runtime, equal K=20, n as shown)

| Slice (source) | n | corpus chunks | EvidenceRecall@20 V1 | V2 (graph) | Δ recall | oracle-availability V1 → V2 | Δ oracle |
|---|---|---|---|---|---|---|---|
| **multi** (ragbench-tests-v1 / HotpotQA) | 10 | 40 | 0.375 | 0.625 | **+0.250** | 0.000 → 0.100 | +0.100 |
| **xquad en+ro** (xquad-en-ro-v1) | 20 | 20 | 0.900 | 0.900 | **+0.000** | 0.850 → 0.850 | +0.000 |
| **tables** (t2-finance-tests-v1 / TAT-DQA) | 10 | 110 | 0.150 | 0.069 | **−0.081** | 0.300 → 0.100 | **−0.200** |
| **belebele-v1** | — | — | **NOT COVERED** — see §6 | | | | |

Reproducibility: 3 timed repeats per slice, recall identical each run (deterministic
fake runtime).

### Interpretation (honest)
- **Multi-hop (HotpotQA) is the only slice where the graph leg helps** (+0.250
  recall). This matches the mechanism claim: entity/relation traversal reaches an
  answer chunk sharing no query terms with the question.
- **xquad (en/ro): no delta.** Its corpus is exactly K (20 chunks), so the baseline
  already retrieves essentially everything — the test has no discriminative power on
  this corpus, it does not show the graph is neutral in general.
- **tables (TAT-DQA): the graph leg HURTS** (−0.081 recall, −0.200 oracle). Numeric /
  table gold contexts are single-chunk-ish and the graph leg's synthesized context
  displaces the table chunk. **This is a real, reportable negative result on a fake
  runtime** and is the opposite of the multi-hop win — the graph is not a uniform
  improvement and the TAT-DQA regression deserves follow-up.

## 4. Latency delta (harness wall-clock, fake adapters)

Function-level `run_paired_eval` wall-clock for K=20, context_top_k=6, averaged
over 3 repeats (includes both arms; absolute values are harness/fake overhead, not
production):

| Slice | corpus chunks | avg wall-clock |
|---|---|---|
| multi | 40 | 254 ms |
| xquad | 20 | 188 ms |
| tables | 110 | 133 ms |

**Per-arm latency delta is NOT separated** by the harness (it reports one combined
wall-clock). The graph leg adds extraction (one-time per corpus) + per-query
traversal; the baseline adds none. On these small corpora the graph leg's extraction
dominates. **No production latency claim.**

## 5. Q9 ground/citation gate (real product code, V1 arm only, gold-context oracle)

`evals/q9_run_benchmark.py` over the same pinned Q8 slices (n=50). This runs the REAL
`verify_claims` + `ExtractiveGrounder` + `score_faithfulness` against a gold-context
oracle. **Retrieval is NOT measured here (gold context is handed in directly) and
there is no graph arm** — it is a grounding/citation-quality check, not a V1-vs-V2
retrieval comparison.

| slice | n | groundedness | citation_validity |
|---|---|---|---|
| en | 10 | 1.0000 | 1.0000 |
| ro | 10 | 1.0000 | 1.0000 |
| pt (pira-v1) | 10 | 0.9853 | 1.0000 |
| tables (TAT-DQA) | 10 | 1.0000 | 1.0000 |
| multi (HotpotQA) | 10 | 0.9586 | 1.0000 |
| **OVERALL** | **50** | **0.9888** | **1.0000** |

Q9 GATE: **PASS** (groundedness 0.9888 ≥ 0.90; citation_validity 1.0000 = 1.0).
This matches the accepted V1 Q9 gate. pt/multi groundedness < 1.0 reflect the
grounder's disclosed cross-language / multi-sentence limits, not fabricated success.

## 6. Coverage / sampling notes

- **Runtime:** ~10 min total, well under the 60-min cap. **No sampling was
  necessary** — every question in the three covered slices was evaluated (n=10/20/10).
- **belebele-v1: NOT COVERED.** It is **absent from the pinned Q8 slices** (the `pt`
  slice is built from `pira-v1`, not belebele). Inspecting `belebele-v1/en/test.jsonl`
  shows a multiple-choice comprehension schema (`mc_answer1..4`, `flores_passage`) with
  **no gold free-text span and no retrieval-relevant chunk refs** — it does not match
  the paired harness's labelled schema (`relevant_chunk_ids` + verbatim `gold_answer`).
  Building a valid paired set from it would require constructing ground-truth chunk
  refs that **do not exist in the source**, i.e. inventing labels. **Declined to
  fabricate; reported as not covered.** Recommend either (a) the coordinator pins a
  belebele slice in Q8 with real chunk refs, or (b) a separate MC-comprehension eval.
- **ragbench-tests-v1** requested as "HotpotQA multi-hop": covered via the `multi`
  Q8 slice, whose `source_path` is the hotpotqa parquet — confirmed.

## 7. What would be needed for a shippable V1-vs-V2 quality claim

1. A **real-runtime** paired harness wiring the production adapters (Qdrant dense +
  PG-FTS lexical + infinity embed/rerank), not fakes — the harness explicitly says this
  is a separate harness.
2. A pinned belebele slice with real chunk references (or exclusion from this gate).
3. Follow-up on the **TAT-DQA regression** (graph leg lowers recall on tables).
4. Latency measured on the real stack, per arm, at production K.

## 8. Files

- Verdict/report: this file.
- Data-prep (throwaway, not product code): `evals/_bench_prepare_paired.py`.
- Derived datasets (temp, not committed): `/tmp/paired_{multi,xquad,tables}.jsonl`.
- Harnesses used unmodified: `evals/graph_paired_eval.py`, `evals/q9_run_benchmark.py`,
  `tests/fixtures/graph_paired_eval.py`.

**Verdict: PARTIAL** — retrieval deltas + Q9 gate measured on a labelled **fake-runtime**
paired basis for 3 of 4 requested slices; grounded-answer rate and production latency
are **not** measured by these harnesses and are not claimed; belebele-v1 not covered
(no real labels; declined to fabricate).
---

## 9. STOP NOTICE (2026-10-12, appended on halt)

Work was halted by user order **after** the benchmark run had already completed and
been written up. Nothing was in flight at halt time: no background commands, no
timers, no running eval/pytest processes (`ps` verified clean).

This file is a **partial/superseded** record. The full report (identical content plus
the numbered sections above) is at `handoffs/HF-BENCH-2026-10-12.md`; this copy exists
because the halt instruction named this path.

**Note for museR:** the coordinator ledger entry in
`handoffs/COORDINATOR-HANDOFF.md` §4 cites the path
`handoffs/HF-BENCH-2026-10-12.md`; it has NOT been rewritten to point here. Treat
that path as canonical unless you prefer otherwise.

**Unchanged and still true after halt:** all figures are **fake-runtime** mechanism
results (not production retrieval), **grounded-answer rate is UNMEASURED** and not
claimed, **per-arm latency is not separable** by the harness, **belebele-v1 is not
covered** (absent from pinned Q8 slices; no real chunk refs — labels not fabricated),
and the **TAT-DQA tables regression** (graph leg recall −0.081, oracle −0.200) stands
as the reportable negative result.

**Repo state:** no commits made; product code untouched (throwaway data-prep script
`evals/_bench_prepare_paired.py` already removed before halt). Working tree holds
only untracked handoff/reviewer files, as expected for an unmerged review stream.
