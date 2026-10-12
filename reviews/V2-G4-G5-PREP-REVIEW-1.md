# V2 G4/G5 preparation — REQUEST CHANGES

Reviewed 2026-10-10: CLINE2-V2 cycle addendum and additive factory/eval/docs.
No merge/deployment authorization; existing scoped graph approval unchanged.

Independent checks: factory + paired-eval selection **14 passed in 0.65s total**;
scoped ruff passed. The handoff incorrectly says 8 factory plus 14 eval tests:
14 is the combined collected count, not the eval-file count. Full 124-test
claim is not independently reverified in this review.

## Findings

1. **P2 — Oracle evidence availability mislabeled grounded-success**
   (`tests/fixtures/graph_paired_eval.py:230`, report summary at 310).
   The metric checks whether the provided gold answer appears in retrieved
   source text. It never invokes an answer engine/provider, inspects a produced
   answer, verifies produced claims/citations, or tests task correctness. It
   would return 1 even if the answering system always returned a wrong answer
   or abstained. Calling a real ExtractiveGrounder does not make this an answer
   success metric. Rename it to an explicit oracle/gold-answer availability
   diagnostic and report grounded-success as unmeasured, or implement actual
   generated-answer/claim/citation evaluation with controlled negative tests.
   Update driver/docs/test labels consistently.

2. **P2 — Loaded workspace silently excluded from both conditions**
   (`tests/fixtures/graph_paired_eval.py:361`, 376, 450, 470, 491).
   Loader accepts arbitrary workspace IDs, but extraction upserts, ACL scope,
   and graph queries all hard-code `ws-g5`. Reproduction: keep the exact default
   synthetic corpus/labels and replace every chunk.workspace_id with
   `company-real`; at context_top_k=20, recall changes from 1.0/1.0 to 0.0/0.0.
   It reports valid-looking zero scores rather than an incompatible dataset.
   Carry explicit per-example authorized workspace scope through both conditions
   or reject unsupported scope at load time. Validate relevant chunk references
   and duplicate/conflicting chunk IDs; do not silently overwrite dataset facts.
   Add a loaded non-default-workspace regression and scope-denial negatives.

3. **P2 — External labels relabel a fake runtime as acceptance-grade evaluation**
   (`tests/fixtures/graph_paired_eval.py:153`, `_index_baseline`, `_make_orchestrator`;
   `docs/graph/G5-EVAL-REPORT.md` final acceptance instructions).
   `--dataset` unconditionally sets synthetic=False and summary becomes only
   `[labelled]`, while dense embeddings are fabricated one-hot vectors and
   stores/reranker remain test fakes. Real labels do not make these the V1
   production retrieval conditions. Tag dataset origin and runtime mode
   independently in EVERY report; fake runtime remains mechanism-only for any
   input. Document required real adapter/provider execution plus frozen dataset
   hash, partition and per-slice paired statistics before G5 acceptance.
   Add loaded-file tests proving runtime disclosure cannot be removed by labels.

## G4 contract clarification

Factory helper has no observed blocker in this scoped review. Protected wiring
is still not implemented or accepted. The integration document says the verifier
resolves graph citations to stored chunks, but current verification trusts items
already in its retrieved map and graph items use synthesized content. Explicitly
require authoritative original-chunk hydration, ACL/version/provenance checks
and exact source spans before grounding graph-derived answers; cite the future
integration test that will prove this. A chunk ID alone is insufficient evidence
of authentic displayed source content. Also correct the statement that no graph
code runs when off: the proposed factory imports graph.factory even when off.

Next authorized task: correct the above prep artifacts and tests in the existing
graph/docs/eval scope; send a precise re-review handoff. No protected G4 wiring,
V3/V4 development, dependency additions, merge or deployment. Q9/G4/G5 remain open.
