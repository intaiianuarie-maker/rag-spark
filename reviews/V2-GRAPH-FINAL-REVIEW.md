# V2 expanded handoff — REQUEST CHANGES

Reviewed 2026-10-09. The previously scoped approval in V2-GRAPH-REREVIEW-3 remains valid for the fixes it covered. This review includes the later GraphStore ABC additions, new MemoryGraphStore deletion methods, SQLite relationship tombstoning, and permanent tests. No merge or final sign-off on this expanded tree.

## P1 — New MemoryGraphStore deletion methods retain deleted community facts

`packages/graph/store.py:156-192` implements the new tombstone contract but never prunes or invalidates communities. `tombstone_document` removes matching entities/relationships while leaving the original community title, summary, and member references; the entity/relationship deletion methods have the same community problem. Global search therefore continues returning deleted facts. The GraphStore ABC explicitly requires affected-community handling for entity/relationship deletion, so the concrete implementations are not interchangeable.

Reproduced with the actual MemoryGraphStore and GraphRetriever:

1. Insert Alice/e with provenance document d, chunk c.
2. Insert a community with entity_ids=[e], title='Alice', summary='Alice secret'.
3. `await store.tombstone_document('ws', 'd')`.
4. `global_search('ws', 'Alice')` still returns `[Alice] Alice secret`.
5. `to_retrieved_items` returns that deleted content as a graph item with `chunk_id=None`.

Fix all three in-memory tombstone paths to invalidate/rebuild affected community content consistently with the SQLite adapter. Include incident relationships when removing entities and ensure deleted provenance cannot re-enter local/global retrieval. Add shared, parameterized lifecycle tests across MemoryGraphStore and SQLiteGraphStore for entity/document/relationship deletion, partial communities, and workspace isolation. Reproduction script: `/tmp/v2_memory_deletion_review.py`.

## Verification

- Original six-suite selection: **89 passed in 4.47s**, outside sandbox. These tests do not cover the in-memory deletion failure above.
- Scoped ruff passed; mypy passed on 10 source files.
- Permanent SQLite edge deletion and all-entity deletion tests exist. The latter exercises empty kept_entities, not the separate no-loaded-surviving-names fallback. Edge-only test checks membership but should also assert the old relationship fact is absent from summary/global results.
- Nonblocking consistency issue: SQLite `tombstone_relationship` returns True even if no row matches, unlike MemoryGraphStore. Align missing-ID semantics and test them.

G4 integration/G5 paired quality evaluation remain pending, as does v1 Q9. Return a graph-only patch for this new deletion defect; only the coordinating reviewer may merge, and no merge is authorized here.
