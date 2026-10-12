# V2 final re-review 2 — REQUEST CHANGES

Reviewed the current graph working tree on 2026-10-09. The ordinary MemoryGraphStore deletion reproduction is fixed: deleting Alice's source removes its community context. Missing SQLite relationship IDs now correctly return False. Previous approvals remain scoped to their reviewed corrections; no merge performed.

## P2 — MemoryGraphStore retains stale content when remaining IDs cannot be resolved

`packages/graph/store.py:225-237` rebuilds a community only when `surviving_names` is nonempty. When `kept_entities` contains unresolved IDs, the list is nonempty but no names load, so the original title/summary remains. The comment claiming the earlier empty-ID path handled this case is incorrect: nonempty IDs and resolvable entities are different conditions. SQLite already deletes this fallback case.

Reproduction using supported store writes (which permit unresolved references): insert a relationship r/document d referencing missing entities; insert a community with entity_ids=['missing'], relationship_ids=['r'], summary='Secret acquisition happened'; tombstone_document(d). Global search for Secret still returns the original summary with no evidence chunks. This can occur when graph members and summaries are written separately or stale references are supplied; the store currently does not enforce referential integrity. Script: `/tmp/v2_memory_fallback_review.py`.

Delete/invalidate the community when no surviving names load, matching SQLite. Cover this exact nonempty-ID/empty-loaded-name state in permanent MemoryGraphStore tests, and add ordinary entity/document/relationship deletion regression coverage for the in-memory adapter. Existing SQLite all-entities-deleted coverage exercises empty IDs, not this fallback. The permanent edge-only SQLite test should also assert removed fact text is absent, not merely that surviving entity names are present.

## Verification

- Combined six-suite selection: **96 passed in 3.69s** outside sandbox.
- Scoped ruff and mypy (10 files) passed.
- `/tmp/v2_memory_deletion_review.py`: original deleted-document reproduction now returns empty context.
- `/tmp/v2_memory_fallback_review.py`: stale fallback reproduced with empty evidence.
- The graph test files currently contain no permanent MemoryGraphStore tombstone tests. The combined count includes concurrently updated v1 answer tests, so the increase from 89 to 96 does not demonstrate seven graph deletion regressions.

G4/G5 and v1 Q9 remain pending. Return the small graph-only fallback fix plus permanent in-memory lifecycle tests for final re-review. No merge or release sign-off.
