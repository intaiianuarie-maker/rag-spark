-- T-V4-018 — DOWN migration (DRAFT, review-only; NOT APPLIED by this ticket)
-- Returns the database to its pre-0001 state: this table and its two indexes
-- gone, every other table untouched.
--
-- ORDER MATTERS: the indexes are dropped before the table because they live on it.
--
-- DATA CAVEAT (reviewer must accept this): DROP TABLE destroys rows. This draft
-- has never been applied, so no live rows exist as of this ticket, and the down
-- path is lossless today. Once a backend has written revisions, ALWAYS export
-- first (see the export query at the bottom of 0001_workflow_draft_revisions.up.sql
-- or use the read-only SELECT below) so nothing is silently lost:
--   SELECT workflow_id, revision, workspace_id, content_hash, spec, summary,
--          parent_revision, parent_hash, created_at
--     FROM workflow_draft_revisions ORDER BY workflow_id, revision;

DROP INDEX IF EXISTS idx_wdr_identity;
DROP INDEX IF EXISTS idx_wdr_workspace;
DROP TABLE IF EXISTS workflow_draft_revisions;

-- Post-rollback verification (read-only):
--   SELECT name FROM sqlite_master WHERE name LIKE 'idx_wdr%';   -- no rows
--   SELECT name FROM sqlite_master WHERE name='workflow_draft_revisions'; -- no rows
--   PRAGMA foreign_key_check;                                     -- clean
