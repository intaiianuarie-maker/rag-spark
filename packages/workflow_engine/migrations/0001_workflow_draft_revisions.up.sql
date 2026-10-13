-- T-V4-018 — UP migration (DRAFT, review-only; NOT APPLIED by this ticket)
-- Persists workflow draft revisions (T-V4-017 in-memory store) to the shared DB.
--
-- STATUS: draft awaiting review. The follow-up backend ticket applies this
-- (directly, or wrapped as an alembic revision on top of 0010_drop_original_bytes).
-- This ticket runs no upgrade and writes nothing to any live database.
--
-- Design notes for the reviewer:
--   * Append-only is enforced by the database, not by convention: the primary key
--     (workflow_id, revision) makes an UPDATE or a re-INSERT of an existing
--     revision impossible, so history cannot be rewritten or dropped by a write.
--   * There is deliberately NO unique constraint on (workflow_id, content_hash):
--     a revert is legal history in the T-V4-017 store (revision 3 may carry the
--     hash of revision 1), so forbidding it here would contradict the store.
--   * Reverting content is looked up by (workflow_id, content_hash), hence that index.
--   * workspace_id is indexed for ACL-scoped reads (R-004: authorization scope is
--     filtered before ranking). No cross-domain FOREIGN KEY: the repo's store
--     precedent (packages/graph/sqlite_store.py) keeps workspace_id as an indexed
--     plain column and enforces scope in the store layer, not in DDL.
--   * spec MUST be stored as the SAME canonical JSON the content_hash is computed
--     from (T-V4-008 canonicalization, which sorts set members). Storing a
--     seed-ordered model_dump() would let identical definitions serialize
--     differently across processes, and a re-hash on read could then fail.
--
-- Idempotent: every statement is IF [NOT] EXISTS, so re-running is a no-op and a
-- partial failure can be retried safely.

CREATE TABLE IF NOT EXISTS workflow_draft_revisions (
  workflow_id     TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  workspace_id    TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  spec            TEXT NOT NULL,
  summary         TEXT NOT NULL DEFAULT '',
  parent_revision INTEGER,
  parent_hash     TEXT,
  created_at      TEXT NOT NULL,
  PRIMARY KEY (workflow_id, revision),
  CHECK (revision >= 1),
  CHECK (length(workspace_id) > 0),
  CHECK (length(content_hash) = 64),
  CHECK (length(spec) > 0),
  CHECK (json_valid(spec)),
  CHECK (parent_revision IS NULL OR parent_revision < revision)
);

-- Workspace-scoped history reads: ACL filter first, then ordering.
CREATE INDEX IF NOT EXISTS idx_wdr_workspace
  ON workflow_draft_revisions (workspace_id, workflow_id, revision);

-- Identity lookups (revise-time idempotency compares the head's hash).
CREATE INDEX IF NOT EXISTS idx_wdr_identity
  ON workflow_draft_revisions (workflow_id, content_hash);

-- Post-apply verification queries (read-only, run by the applying ticket):
--   SELECT COUNT(*) FROM workflow_draft_revisions;                       -- 0 on a fresh install
--   SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_draft_revisions';
--   PRAGMA table_info(workflow_draft_revisions);                          -- 9 columns, PK (workflow_id, revision)
--   PRAGMA index_list(workflow_draft_revisions);
