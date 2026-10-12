# V2 graph re-review 3 — APPROVE scoped corrections

Reviewed 2026-10-09, current graph working tree over HEAD `00579ebda8b7e06bf936444bfa1f2ac1c2c83332`. This approves the corrections to the findings in V2-GRAPH-REREVIEW-2, not a merge or completed v2 release. No merge performed.

## Findings closed

- Source-name validation requires the complete ordered normalized phrase with word boundaries; absent surnames, substring names, recombined names, and reordered tokens are rejected.
- Flag-off parity compares the full `model_dump(mode="json")` item lists, including scores, content, metadata, scope and provenance, against nonempty HYBRID output on deterministic fixtures.
- The synthetic traversal regression uses the same corpus and K=20 budget in both conditions and no longer forces a recall advantage. Independent equal-budget reproduction yields equal recall in this fixture: 2/3 at K=2, 1 at K=20. This is not a G5 quality benchmark.
- Partial community deletion regenerates title/summary from surviving names, with no stale-content fallback when no names remain. The original Alice/Bob deletion reproduction is fixed. Independent SQLite content checks also pass for a true edge-only document deletion (entities survive; deleted relationship facts disappear) and for no surviving entity names (community tombstoned).

## Verification

- `/root/ko-venv/bin/python -m pytest -q tests/unit/test_graph_extractor.py tests/unit/test_graph_store_sqlite.py tests/unit/test_graph_retrieval_eval.py tests/unit/test_orchestrator.py tests/unit/test_answer_engine.py tests/unit/test_graph.py`: **88 passed in 2.82s**, outside sandbox. This combined count includes the current v1 answer suite; it is not graph-only.
- Scoped ruff: passed.
- Scoped mypy: passed, 10 source files.
- `/tmp/v2_rereview_repro.py`: recombined name rejected; equal-K recall measured.
- `/tmp/v2_edge_review.py`: edge-only invalidation and no-surviving-names content checks passed. Database callbacks run synchronously in this focused script to isolate content semantics; normal async store behavior is covered by the pytest run.

## Limits and nonblocking follow-up

The new test labelled edge-only actually removes an entity and its incident edge. Promote the independent true edge-only and no-surviving-names checks into permanent regression tests. The baseline recall assertion `>= 0.0` adds no regression protection; replace it with the fixture's measured expected recall. These are test-strength follow-ups, not newly observed runtime failures.

G4 API/UI/factory integration and G5 paired Q8 quality evaluation remain pending. No live release/acceptance probes were run, and v1 Q9 has not been accepted. Keep the branch unmerged under the plan seam and the user's explicit instruction. Approval covers the reviewed graph fixes only, not concurrent v1 changes.
