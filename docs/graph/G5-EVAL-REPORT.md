# G5 — Paired graph-vs-baseline evaluation report (V2)

> Status: **harness + synthetic fixtures complete; real paired quality eval
> BLOCKED on a missing labelled multi-hop dataset.** This report is honest about
> what is and is not established.
>
> Author: dev2 (Stream B) · 2026-10-10 · branch `feat/v2-graph`.

## What G5 requires (V2-GRAPH-PLAN §2)

> graph-vs-baseline on Q8 multi-hop/multi-source sets; EvidenceRecall@20 and
> grounded-success deltas reported paired; no holdout tuning.

A genuine paired quality evaluation needs a **frozen, labelled multi-hop /
multi-source corpus** (questions → gold evidence chunks → gold grounded answer)
with a preserved source-family dev/holdout partition. Per the delivery plan,
this is a prerequisite, not an assumption.

## Honest dataset status — BLOCKER

**No labelled multi-hop/multi-source eval corpus exists in the tree.** What is
present:

- `evals/golden.jsonl` — 2 v1 rows (termination-notice, supplier). Single-hop,
  single-source; not multi-hop; not graph-relevant.
- `evals/golden_smoke.jsonl` — v1 smoke rows incl. one 2-doc merge row, but no
  gold *chunk* ids and no graph structure; used to prove the v1 eval harness,
  not graph quality.
- `datasets/rag-eval/t2-finance-tests-v1/` — T2-RAGBench (FinQA / ConvFinQA /
  TAT-DQA), CC-BY-4.0. This is **financial table+text QA**, not a multi-hop
  entity/relation benchmark, and it ships **no gold evidence-chunk labels** in
  the graph sense. It is a candidate *source* for building labelled fixtures,
  but it is **not** itself a ready G5 set and using it as one would be a
  fabricated-quality claim.

Therefore this cycle delivers the **harness + fixtures** and reports the
**mechanism** result on a synthetic corpus, while explicitly refusing to claim a
production quality gain. Real Q8 multi-hop labels are the required input for the
acceptance-grade paired eval.

## Harness

- `tests/fixtures/graph_paired_eval.py` — paired, equal-K evaluator. Runs the
  real offline extraction/retrieval pipeline: `RuleBasedGraphExtractor` (G1) →
  `MemoryGraphStore` (G2) → `GraphRetriever` (G3), against a `FakeVectorStore` +
  `FakeLexicalStore` baseline over the **same** chunks.
- **Two independent disclosure tags on every report:** `dataset_origin`
  (`synthetic` | `labelled`) and `runtime_mode` (`fake` | `real`). The runtime is
  **always `fake`** here (one-hot fake embeddings, in-memory fake stores,
  token-overlap fake reranker); real labels never upgrade it.
- **grounded-success is UNMEASURED.** No answer engine/provider is invoked and no
  produced answer/claim/citation is inspected. The harness reports **oracle
  gold-answer availability** (`oracle_gold_answer_availability`, real v1
  `ExtractiveGrounder` applied to the *gold* answer vs retrieved source) as a
  retrieval diagnostic — explicitly **not** answer success (it would score 1.0
  even if the system always answered wrongly or abstained).
- **Workspace scope is threaded, not hard-coded.** The dataset's authorized
  workspace (`dataset.workspace_id`, or a per-example override) flows through
  extraction, ACL scope, and every graph query, so a non-default workspace is
  evaluated (1/1), never silently excluded (0/0). Out-of-scope queries fail
  closed.
- **Loader validation** (`load_labelled_dataset`) rejects dangling chunk
  references, conflicting duplicate chunk ids, and multi-workspace corpora
  (`DatasetValidationError`) rather than reporting valid-looking zero scores; it
  returns `None` only when the file is missing/empty.
- `tests/unit/test_graph_paired_eval.py` — **14-test** regression lock (below).
- `evals/graph_paired_eval.py` — standalone driver; prints the paired report at
  the orchestrator default window and a wide window; accepts `--dataset` for a
  real labelled set and **refuses to fabricate numbers** when it is absent or
  incompatible.

## Measured result (synthetic, deterministic) — mechanism, NOT a quality claim

Corpus: `Alice -WORKS_FOR-> Acme -OWNS-> Globex -produces-> Zephyr`, 3 relevant
chunks + 8 keyword near-miss distractors mentioning "Acme Corporation", K=20.
The 2-hop answer chunk `ch-prod` shares no content word with the query.
**grounded-success is UNMEASURED** on every row; the numbers below are
EvidenceRecall and oracle gold-answer availability only.

**Leg level (the honest, stable signal):**

| Condition | EvidenceRecall@20 | Notes |
|---|---|---|
| Graph leg (`GraphRetriever.local_search`) | **1.000** | traverses entity/relation provenance to `ch-prod` |
| Keyword/lexical baseline | **0.667** | cannot reach `ch-prod` (no token overlap) |
| **Paired delta** | **+0.333** | graph leg beats keyword retrieval on the multi-hop chunk |

**Fused orchestrator level (`HYBRID_GRAPH` vs `HYBRID`) — reported, no gain claimed:**

| Context window | Baseline recall | Graph recall | Delta |
|---|---|---|---|
| default `context_top_k=6` | 0.333 | 0.333 | 0.000 (graph evidence truncated) |
| wide `context_top_k=20` | 1.000 | 1.000 | 0.000 (small-corpus ceiling; both saturate) |
| wide + 40 distractors | 0.667 | 0.333 | **−0.333** (graph leg diluted under RRF) |

### Why the fused path shows no gain (and why that is honest)

1. **Uniform graph scores.** `GraphRetriever.to_retrieved_items` emits one item
   per evidence chunk, all with the same score, so RRF cannot rank the multi-hop
   answer chunk above near-miss keyword hits.
2. **Default context window.** `context_top_k=6` truncates the fused set before
   the graph-surfaced chunk survives.
3. **Small-corpus ceiling.** With corpus ≤ K the baseline trivially retrieves
   everything, so equal-K recall is degenerate — the exact failure mode the
   reviewer flagged ("synthetic equal-K traversal tests do not establish a
   quality gain").

These are reproducible facts (see the test's window cases), not a claimed
improvement. Fixing (1)/(2) is a post-Q9 follow-up, tracked in
[`G4-INTEGRATION-CONTRACT.md`](./G4-INTEGRATION-CONTRACT.md) §3.

## Regression lock (all green)

```
tests/unit/test_graph_paired_eval.py — 14 passed
```

Pinned invariants: graph leg surfaces the multi-hop chunk while the keyword
baseline misses it; oracle gold-answer availability is a diagnostic (real
grounder) and grounded-success is reported UNMEASURED; a **loaded non-default
workspace** is evaluated (1/1) not silently excluded (0/0); an out-of-scope
workspace fails closed; the loader rejects dangling refs / conflicting duplicate
chunk ids / multi-workspace corpora; a labelled dataset does **not** remove the
FAKE-runtime disclosure; a wide window recovers graph evidence in fusion
(truncation, not a retrieval failure); a missing dataset returns `None`.

## To reach an acceptance-grade G5

1. Obtain/build a **frozen labelled multi-hop/multi-source** corpus with gold
   evidence-chunk ids + gold grounded answers, preserving the source-family
   dev/holdout partition (do **not** impose a naive 80/20). Record a frozen
   dataset hash.
2. Run on the **REAL runtime** (real embedding/reranker providers and real
   dense/lexical stores behind the orchestrator — not these fakes) with equal K
   and disclosed retrieval budgets, tagging `runtime_mode=real`.
3. Evaluate **actual produced answers**: invoke the answer engine, parse its
   claims, verify each claim/citation against hydrated original chunks
   ([`G4-INTEGRATION-CONTRACT.md`](./G4-INTEGRATION-CONTRACT.md) §2a), and report
   grounded-success (produced-answer) deltas paired, per-slice, with confidence
   intervals — alongside EvidenceRecall@K. Keep the oracle diagnostic separate
   and clearly labelled.
4. Address the fusion-scoring limitation (§3) so the fused path — not only the
   leg — reflects the graph advantage, then re-run paired.

> **Manifest/partition invariant (enforced by `evals/graph_dataset_manifest.py`):**
> the built manifest stores every source path **relative to the common datasets
> root** and **rejects duplicate relative paths** rather than silently
> overwriting a family mapping (this fixes the FinQA/TAT-DQA `test/metadata.jsonl`
> collision where two unrelated families previously shared one manifest key and
> one hash overwrote the other). Partitioning is validated family-by-family by
> `validate_partitions`, which reports overlapping families (leakage), unknown
> assigned ids, unassigned families, and duplicate partition names; a report is
> `evaluation_ready` only when it is structurally clean **and** the `holdout`
> partition is non-empty (an empty/missing holdout is inventory-only).

Until (1)–(4), G5 remains **mechanism-demonstrated, quality-unproven**; the fake
runtime is mechanism-only for any input, labelled or not.
