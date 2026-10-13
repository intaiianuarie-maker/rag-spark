# T-V4-018 — Revision-persistence migration DRAFT (review-only)

**Status: DRAFT — reviewed here, applied by nobody.** This ticket contains no
alembic revision, runs no upgrade, and writes to no live database. The two SQL
files beside this document are the artefacts the follow-up backend ticket will
execute once this draft is accepted:

- `0001_workflow_draft_revisions.up.sql`
- `0001_workflow_draft_revisions.down.sql`

The covering tests (`tests/unit/test_workflow_draft_migration.py`) apply both
directions **only** against scratch SQLite files in `tmp_path`, never against a
live database, and assert the downgrade returns the scratch DB to its exact
prior state.

---

## 1. Why this migration exists

T-V4-017 delivered a versioned draft/revision store **in memory** behind a
`DraftStore` ABC. The W4 approval gates (T-V4-019+) decide things about *one
specific revision*, so a revision that only exists in a process's heap cannot be
the object of an approval: a restart would erase the artefact the approval
referred to. Persisting revisions is therefore the prerequisite for every
approval gate after this one.

The card requires the durable path be **reviewed before it is built**, which is
why this ticket is a document plus a downgrade-tested SQL pair rather than a
backend implementation.

## 2. Schema statement

One new table. No existing table is altered, and no existing column changes
type, so this migration is purely **additive** and safe to re-run.

```sql
CREATE TABLE IF NOT EXISTS workflow_draft_revisions (
  workspace_id    TEXT NOT NULL,
  workflow_id     TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  content_hash    TEXT NOT NULL,
  spec            TEXT NOT NULL,
  summary         TEXT NOT NULL DEFAULT '',
  parent_revision INTEGER,
  parent_hash     TEXT,
  created_at      TEXT NOT NULL,
  PRIMARY KEY (workspace_id, workflow_id, revision),
  CHECK (revision >= 1),
  CHECK (length(workspace_id) > 0),
  CHECK (length(workflow_id) > 0),
  CHECK (length(content_hash) = 64),
  CHECK (length(spec) > 0),
  CHECK (json_valid(spec)),
  CHECK (parent_revision IS NULL OR parent_revision < revision)
);
```

**Columns**

| column | type | null | meaning |
|---|---|---|---|
| `workspace_id` | TEXT | no | owning workspace; the ACL scope (R-004) and **part of the key** |
| `workflow_id` | TEXT | no | draft workflow identity (the `WorkflowDefinition.id`) |
| `revision` | INTEGER | no | 1-based, monotonic per workflow, never reused |
| `content_hash` | TEXT | no | canonical SHA-256 of `spec`, per T-V4-008; exactly 64 hex chars |
| `spec` | TEXT | no | the definition as **canonical JSON** (see §5 hazard) |
| `summary` | TEXT | no | bounded, redacted one-line projection from the store |
| `parent_revision` | INTEGER | yes | revision this one supersedes; NULL for revision 1 |
| `parent_hash` | TEXT | yes | parent's content hash — the chain that makes tampering visible |
| `created_at` | TEXT | no | ISO-8601 UTC timestamp |

**Constraints**

- `PRIMARY KEY (workspace_id, workflow_id, revision)` **hardened in T-V4-020** —
  this is the append-only guarantee at
  the storage layer *and* the workspace-isolation guarantee: an existing
  revision cannot be UPDATEd or re-INSERTed without a constraint violation, so
  history cannot be rewritten or dropped by an application bug. It also
  subsumes the monotonic-number invariant: a duplicate or backwards revision
  number is refused by the database rather than trusted to the caller. Because
  `workspace_id` is part of the key, two workspaces that use the same workflow
  id cannot collide, and a cross-workspace row is unreachable by construction —
  scope is not a query someone may forget to write.
- `CHECK (revision >= 1)` — no zero/negative revision numbers.
- `CHECK (length(content_hash) = 64)` — a full SHA-256 digest, not a prefix.
- `CHECK (json_valid(spec))` — `spec` is parseable JSON at write time. **The
  applying ticket must confirm its SQLite build has JSON1 enabled** (the
  covering tests assert it for the environment this draft was written in).
- `CHECK (parent_revision < revision)` — the parent chain always points
  backwards, so a cycle or self-parent is impossible.
- `CHECK (length(workspace_id) > 0)` — a revision may never be scope-less.
- `CHECK (length(workflow_id) > 0)` — a revision may never be id-less.

**Indexes**

| index | columns | serves |
|---|---|---|
| `idx_wdr_workspace` | `(workspace_id, workflow_id, revision)` | workspace-scoped history reads, ACL filter first (R-004) |
| `idx_wdr_identity` | `(workflow_id, content_hash)` | identity lookups: is this content already stored? |

**Deliberately absent**

- **No `UNIQUE (workflow_id, content_hash)`.** A revert is legal history — the
  T-V4-017 store allows revision 3 to carry revision 1's hash — so forbidding it
  here would contradict the in-memory store's verified semantics. Identity is
  looked *up* by that pair, not *enforced* unique by it.
- **No cross-domain `FOREIGN KEY` to `workspaces`.** Follows the repo's store
  precedent (`packages/graph/sqlite_store.py` keeps `workspace_id` an indexed
  plain column) and keeps this table deployable into a workflow-local database
  as well as the shared one. Scope enforcement stays in the store layer, where
  R-004 puts it.
- **No trigger.** History protection is a constraint, not a trigger: triggers
  are harder to reason about, harder to downgrade, and add a write path.

## 3. Up / down

**Up** — the statements in §2, all `IF NOT EXISTS`/`IF EXISTS`, so the
migration is idempotent and a partially failed apply can be retried.

**Down** — in dependency order, because an index lives on its table:

```sql
DROP INDEX IF EXISTS idx_wdr_identity;
DROP INDEX IF EXISTS idx_wdr_workspace;
DROP TABLE IF EXISTS workflow_draft_revisions;
```

Returns the database to its pre-0001 state: the new table and its two indexes
are gone and every other object is untouched. Both directions are executed in
the covering tests against a scratch DB, and the test asserts the *full*
`sqlite_master` snapshot after down equals the snapshot before up — not merely
that the table is absent.

## 4. Backfill story — none needed, and why

**Existing draft content is in-memory only, so there is nothing to backfill.**

1. The T-V4-017 store holds revisions in a Python dict for the lifetime of one
   process. It has never written a row anywhere, in any environment.
2. There is therefore no durable corpus to migrate: a backfill would have no
   source to read from and no destination rows to preserve.
3. The durable backend starts empty and fills forward — the first revision
   written after cutover is revision 1, and the in-memory store remains the
   source of truth until the backend ticket cuts over.
4. This is also why the migration carries no `INSERT`/`UPDATE`/`DELETE`
   statements at all: the only way a draft revision enters this table is by
   being created through the store. A reviewer can verify the claim by reading
   the SQL — the up file contains three `CREATE` statements and nothing else.

If a rollback is ever needed **after** a backend has written rows, that is a
different story and the down migration says so in its own header: export first
(the read-only `SELECT` in the down file), then drop. The migration is lossless
today precisely because it has never been applied.

## 5. Rollback story

1. **Before applying:** the down file is the rollback. Nothing has been written,
   so rollback is a no-op that costs nothing.
2. **After applying, before any rows:** run the down file. Table and indexes
   disappear; the pre-up state is restored exactly (proven in the covering
   tests against scratch SQLite).
3. **After applying, with rows:** export the table first (the `SELECT` at the
   bottom of the down file), then run the down file. The draft content then
   survives outside the database as JSON and can be re-imported if the durable
   backend is ever reinstated.
4. **Never** roll back by hand-editing rows: the primary key makes history
   append-only, and hand edits would leave `parent_hash` chains pointing at
   revisions that no longer verify.

## 6. The one hazard the follow-up backend must handle

**`spec` must be stored in the canonical form the hash was computed from.**

`WorkflowDefinition.workspace_capabilities` is a `set`. A plain
`model_dump(mode="json")` serializes it in **iteration order**, which follows
`PYTHONHASHSEED` — so two processes can produce different bytes for identical
content. The hash itself is safe (T-V4-008 canonicalizes sets before hashing),
but if the stored `spec` were a seed-ordered dump, then re-hashing a row on read
could disagree with its `content_hash` even though nothing changed.

The backend must therefore persist
`json.dumps(WorkflowRevision._canonical_payload(spec.model_dump()), sort_keys=True, default=str)`
— the same input the hash consumes — or store the spec as produced by the
canonicalizer. The covering tests include a round-trip that pins this
requirement (write a definition containing a set under several
`PYTHONHASHSEED` values, read it back unchanged, re-hash, and require equality),
because it is the one way a "working" backend could silently corrupt identity.

## 7. Review checklist for the applying ticket

- [ ] Confirm the target SQLite build has JSON1 (`SELECT json_valid('{}')`).
- [ ] Register the migration with the shared-DB toolchain (an alembic revision
      on top of `0010_drop_original_bytes`, or the equivalent) — **not** part of
      this draft, since this ticket must not touch alembic versions or applied
      state.
- [ ] Apply up; verify the three post-apply queries in the up file.
- [ ] Verify `PRAGMA table_info(workflow_draft_revisions)` shows the primary key
      on `(workspace_id, workflow_id, revision)` and 9 columns.
- [ ] Confirm `alembic_version` advanced by exactly one revision and no other
      revision file was modified.
- [ ] Build the durable `DraftStore` behind the T-V4-017 ABC (it must stay the
      only interface the rest of the code sees).
- [ ] Run the T-V4-017 suite unchanged against the durable backend — same ABC,
      same expectations.
- [ ] Keep the `REVIEWED` status in this file until the applying ticket marks it
      `APPLIED`, and record the commit that applied it.

## 8. Proof that nothing ran

- No `.py` file exists under `packages/workflow_engine/migrations/`, so alembic
  cannot import a revision from here; alembic's `versions/` directory is
  untouched by this ticket (asserted in the covering tests).
- The covering tests connect only to SQLite files created in `tmp_path` and
  never to any configured live database path.
- Live-DB writes from this ticket: **0**.
