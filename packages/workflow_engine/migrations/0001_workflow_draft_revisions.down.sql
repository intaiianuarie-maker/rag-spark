-- T-V4-018 / T-R18 — DOWN migration (DRAFT, review-only; NOT APPLIED by this ticket)
-- Returns the database to its pre-0001 state: tables, indexes, and triggers
-- gone, every other table untouched.
--
-- ORDER MATTERS: triggers are dropped before indexes and tables because they live on them.

DROP TRIGGER IF EXISTS trg_wab_no_delete;
DROP TRIGGER IF EXISTS trg_wab_no_update;
DROP INDEX IF EXISTS idx_wab_lookup;
DROP TABLE IF EXISTS workflow_approval_bindings;

DROP TRIGGER IF EXISTS trg_wdr_monotonic_insert;
DROP TRIGGER IF EXISTS trg_wdr_no_delete;
DROP TRIGGER IF EXISTS trg_wdr_no_update;
DROP INDEX IF EXISTS idx_wdr_identity;
DROP INDEX IF EXISTS idx_wdr_workspace;
DROP TABLE IF EXISTS workflow_draft_revisions;

-- Post-rollback verification (read-only):
--   SELECT name FROM sqlite_master WHERE name LIKE 'idx_wdr%' OR name LIKE 'trg_wdr%';   -- no rows
--   SELECT name FROM sqlite_master WHERE name='workflow_draft_revisions'; -- no rows
--   PRAGMA foreign_key_check;                                     -- clean
