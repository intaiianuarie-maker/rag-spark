# G5 REAL-adapter paired runner — execution contract (V2)

> Status: **contract prepared; the runner is NOT implemented here.** The current
> `tests/fixtures/graph_paired_eval.py` runs the **fake** runtime (mechanism
> only). A real paired eval requires wiring the **production** adapters, which
> touches the protected retrieval/answer seam and needs Q9 + coordinator
> dispatch. This document is the reviewed contract so that work is mechanical
> once unblocked. No production ingestion/service calls, no new dependencies, no
> protected edits, and **no holdout tuning** are performed in this cycle.
>
> Author: dev2 (Stream B) · 2026-10-11 · branch `feat/v2-graph`.

## 1. Goal

Run the **same** paired, equal-K graph-vs-baseline comparison as the fake
harness, but on the **real** runtime, and report **grounded-success of produced
answers** (not the oracle diagnostic), per-slice, with confidence intervals —
against the frozen, family-partitioned labelled corpus from
[`G5-SOURCE-INVENTORY.md`](./G5-SOURCE-INVENTORY.md).

## 2. Runtime disclosure (non-negotiable)

The real runner must set `runtime_mode="real"` **only because it constructs the
real adapters** (the disclosure is derived from what runs, exactly as the fake
runner derives `fake`). It must NOT reuse the fake `run_paired_eval` override
path (that was removed). The two tags stay independent:

- `dataset_origin` = `labelled` (real labelled corpus) or `synthetic`.
- `runtime_mode` = `real` (real adapters) — reported truthfully, never asserted.

Every report line carries both. A real run on a synthetic corpus is still
`synthetic`-origin; a fake run on real labels is still `fake`-runtime.

## 3. Adapters to wire (behind existing interfaces — no vendor leak)

| Leg | Real adapter (already in tree) | Interface |
|---|---|---|
| Dense | `retrieval.adapters.qdrant_store.QdrantVectorStore` | `VectorStore` |
| Embedding | `retrieval.adapters.infinity_embed.InfinityEmbeddingProvider` | `EmbeddingProvider` |
| Lexical | `retrieval.adapters.pg_fts.PostgresLexicalStore` | `LexicalStore` |
| Rerank | `retrieval.adapters.infinity_rerank.InfinityReranker` | `Reranker` |
| Graph store | `graph.sqlite_store.SQLiteGraphStore` (or future Neo4j adapter) | `GraphStore` |
| Graph build | `graph.factory.create_graph_retriever()` + worker extraction (G1) | — |
| Answer + grounding | `answer.engine.AnswerEngine` + `answer.verification` | — |

The runner constructs the orchestrator via the same seam the product uses
(`answer.factory.create_orchestrator` post-G4) so the eval measures the real
path, not a parallel one.

## 4. Equal budgets (paired fairness)

- Both conditions index the **same** corpus chunks (corpus membership held
  constant) and use the **same** K for dense/lexical/sparse/fusion and the same
  `context_top_k`.
- Disclose the exact budget (K, context_top_k, rerank on/off, top-k per leg) in
  the run snapshot. Baseline = `HYBRID` (no graph); treatment = `HYBRID_GRAPH`.
- Ingestion is done **once**, offline, into the real stores before the run (the
  contract forbids live ingestion/service calls *during* scoring; a one-time
  offline index build against a local/dev stack is the setup, disclosed as such).

## 5. Metrics

Per example, per condition, per slice:

- **EvidenceRecall@K** — relevant gold chunk ids in top-K (retrieval quality).
- **grounded-success (produced answer)** — invoke the `AnswerEngine`, parse its
  claims, and verify each claim/citation against the **hydrated original chunk**
  (see G4 §2a). Success = the answer is non-abstaining AND every surviving claim
  is grounded in an in-scope, in-version original chunk with a located span.
  This is the real answer-success metric the fake harness could not measure.
- **oracle gold-answer availability** — keep as a separate, clearly-labelled
  diagnostic; never conflate with grounded-success.
- Report paired deltas (treatment − baseline) per slice.

## 6. Per-slice + confidence intervals

- Define slices from the corpus metadata BEFORE looking at outcomes (no holdout
  tuning): e.g. `n_hops` (1 vs ≥2), `n_sources` (single vs multi-document),
  `answer_type` (numeric/text), `language` (EN/RO/PT), `table_vs_text`.
- Report mean ± **95% CI** per slice using a paired bootstrap over examples
  (resample example ids with replacement, recompute the mean paired delta;
  percentile CI). Disclose n per slice; slices with n < a stated floor (e.g. 30)
  are reported as **underpowered**, not dropped silently.
- Freeze and record the dataset hash (from the manifest) so CIs are reproducible.

## 7. Leakage & honesty gates (must pass before any number is reported)

1. `validate_partitions(dev, holdout)` → **ok** (no family in both splits).
2. `validate_gold_refs(all gold chunk/source refs, manifest)` → **ok** (no
   dangling labels; every cited source is present).
3. Manifest re-hash matches the frozen manifest (no corpus drift).
4. Runtime disclosure is `real` **and** derived from constructed adapters.
5. Holdout is scored **once**; no re-tuning on holdout. Any slice/threshold
   change invalidates prior holdout numbers and must be re-run on a fresh split.

## 8. Future test (protected seam, post-Q9)

`tests/integration/test_graph_paired_real.py` — asserts the equal-budget
snapshot, per-slice CI computation, the leakage gates (1)–(3), and that a
graph-derived citation hydrates the original chunk (G4 §2a). Until it is green,
G5 acceptance numbers are **not** produced. This cycle adds no integration test
(protected seam) — only this contract and the fake-runtime unit locks.

## 9. Out of scope here

Real adapter wiring, ingestion, service calls, dependency additions, protected
edits, holdout tuning, and any G5 acceptance claim. All remain gated on Q9 +
coordinator dispatch.
