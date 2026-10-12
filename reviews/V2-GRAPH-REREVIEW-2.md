# V2 graph re-review 2 — REQUEST CHANGES

Reviewed the current working-tree graph changes on 2026-10-09. No merge or release approval.

The original Alice/Bob partial-deletion reproduction is fixed: deleting Alice leaves no global result for Alice. The original invented-surname and substring examples are also rejected now. Three findings remain unresolved:

1. **P2 — Name validation still accepts invented combinations of separate mentions.** `packages/graph/adapters/lightrag_extractor.py:119-130` treats the source as an unordered token set. `_name_in_source("Alice Smith", "Alice Jones met Bob Smith.")` returns True although Alice Smith never appears. This also accepts reordered or arbitrarily repeated tokens. Match the full normalized contiguous token sequence with word boundaries, or resolve only explicit validated aliases. Add recombined-name/order negatives through the extractor, not just absent-token negatives.

2. **P2 — Parity checks only chunk IDs, not serialized output.** `tests/unit/test_graph_retrieval_eval.py:358-362` ignores content, workspace/provenance, metadata, score maps, and other returned fields; its docstring's claim of serialized comparison is false. Compare full deterministic serialized items between independently constructed HYBRID and graph=None/HYBRID_GRAPH fixtures. Do not exclude scores/ranks as nondeterministic when these fake fixtures and RRF are deterministic. Keep the nonempty assertions; cover reranking/degradation as relevant. Chunk-ID parity is useful but does not establish byte-identical behavior.

3. **P2 — Recall comparison now uses unequal K/result budgets.** `tests/unit/test_graph_retrieval_eval.py:221-242` compares graph Recall@20/fusion_top_k=20 against baseline Recall@2/fusion_top_k=2. With three relevant chunks, the latter is mathematically capped at 2/3. Matching corpora fixed one confound but introduced another. Independent reproduction with equal budgets yields:

   | Result budget / recall K | Graph recall | Hybrid recall |
   |---|---|---|
   | 2 | 0.6667 | 0.6667 |
   | 20 | 1.0 | 1.0 |

   Use the same output K and documented comparable candidate settings in both conditions. A valid result may show no gain; do not force an advantage. Keep this as a synthetic traversal/fusion regression and leave G5 pending until paired Q8 EvidenceRecall@20 and grounded-success evaluation exists.

## Verification and limits

- Six-suite selection from the original handoff: **87 passed in 2.24s**, outside sandbox.
- Scoped ruff passed; mypy passed on 10 source files.
- The six-suite total includes concurrently revised v1 answer tests; it is not a count of graph-only regression cases.
- `/tmp/v2_rereview_repro.py` records invented-name recombination and equal-K results. `/tmp/v2_review_repro.py` confirms the original deletion/name examples are fixed.
- `tests/unit/test_graph_store_sqlite.py` still lacks a partial-deletion assertion on summary/title/global output and an edge-only invalidation regression. Add those to preserve the summary fix. If no valid surviving names can be loaded, tombstone the community instead of retaining the old title/summary fallback at sqlite_store.py:384-389.
- No live integration/acceptance release probes performed. G4 remains pending under the v1 Q9 seam.

Return a precise graph-only diff/commit and corrected test evidence for another review. Do not merge or edit the v1 stream.
