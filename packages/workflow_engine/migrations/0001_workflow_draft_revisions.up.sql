-- T-V4-018 / T-R18 — UP migration (DRAFT, review-only; NOT APPLIED by this ticket)
-- Persists workflow draft revisions (T-V4-017 in-memory store) to the shared DB.
--
-- STATUS: draft awaiting review. The follow-up backend ticket applies this
-- (directly, or wrapped as an alembic revision on top of 0010_drop_original_bytes).
-- This ticket runs no upgrade and writes nothing to any live database.
--
-- Design notes for the reviewer:
--   * Append-only is enforced at the persistence layer by immutable triggers (remedying F13):
--     trg_wdr_no_update and trg_wdr_no_delete reject all UPDATE and DELETE operations
--     with typed errors, while trg_wdr_monotonic_insert enforces strictly monotonic
--     allocation (rejecting backwards and out-of-order inserts). The composite primary key
--     (workspace_id, workflow_id, revision) prevents duplicate key collision, but
--     triggers provide the actual append-only mutation block.
--   * Workspace isolation is enforced by the store layer and scoped queries, NOT by the primary key alone:
--     Including workspace_id in the composite primary key ensures separate workspaces with the
--     same workflow_id do not collide at the key level, but the table remains multi-tenant.
--     True isolation requires explicit query filtering (WHERE workspace_id = ?) and authorization
--     checks in the store layer before data is served (correcting the false isolation claim).
--   * Approval bindings bind workspace + hash + policy:
--     workflow_approval_bindings strictly requires workspace_id, content_hash (64 hex),
--     and policy_revision (non-empty), ensuring approvals cannot silently apply to modified
--     workflow definitions or changed policies.
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

-- Workspace-scoped history reads: ACL filter first, then ordering.
CREATE INDEX IF NOT EXISTS idx_wdr_workspace
  ON workflow_draft_revisions (workspace_id, workflow_id, revision);

-- Identity lookups (revise-time idempotency compares the head's hash).
CREATE INDEX IF NOT EXISTS idx_wdr_identity
  ON workflow_draft_revisions (workflow_id, content_hash);

-- Append-only enforcement triggers (F13 remediation)
CREATE TRIGGER IF NOT EXISTS trg_wdr_no_update
BEFORE UPDATE ON workflow_draft_revisions
BEGIN
  SELECT RAISE(ABORT, 'workflow_draft_revisions is append-only: UPDATE prohibited');
END;

CREATE TRIGGER IF NOT EXISTS trg_wdr_no_delete
BEFORE DELETE ON workflow_draft_revisions
BEGIN
  SELECT RAISE(ABORT, 'workflow_draft_revisions is append-only: DELETE prohibited');
END;

CREATE TRIGGER IF NOT EXISTS trg_wdr_monotonic_insert
BEFORE INSERT ON workflow_draft_revisions
BEGIN
  SELECT CASE
    WHEN (SELECT COUNT(*) FROM workflow_draft_revisions WHERE workspace_id = NEW.workspace_id AND workflow_id = NEW.workflow_id) = 0
         AND (NEW.revision != 1 OR NEW.parent_revision IS NOT NULL OR NEW.parent_hash IS NOT NULL)
    THEN RAISE(ABORT, 'workflow_draft_revisions initial revision must be 1 with no parent')
    WHEN (SELECT COUNT(*) FROM workflow_draft_revisions WHERE workspace_id = NEW.workspace_id AND workflow_id = NEW.workflow_id) > 0
         AND (
           NEW.parent_revision IS NULL
           OR NEW.parent_hash IS NULL
           OR NEW.revision != (SELECT MAX(revision) + 1 FROM workflow_draft_revisions WHERE workspace_id = NEW.workspace_id AND workflow_id = NEW.workflow_id)
           OR NEW.parent_revision IS NOT (SELECT MAX(revision) FROM workflow_draft_revisions WHERE workspace_id = NEW.workspace_id AND workflow_id = NEW.workflow_id)
           OR NEW.parent_hash IS NOT (SELECT content_hash FROM workflow_draft_revisions WHERE workspace_id = NEW.workspace_id AND workflow_id = NEW.workflow_id AND revision = (SELECT MAX(revision) FROM workflow_draft_revisions WHERE workspace_id = NEW.workspace_id AND workflow_id = NEW.workflow_id))
         )
    THEN RAISE(ABORT, 'workflow_draft_revisions is append-only: out-of-order or backwards revision insertion prohibited')
  END;
END;

-- Workflow approval bindings (workspace + hash + policy)
CREATE TABLE IF NOT EXISTS workflow_approval_bindings (
  workspace_id    TEXT NOT NULL,
  workflow_id     TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  content_hash    TEXT NOT NULL,
  policy_revision TEXT NOT NULL,
  approver_id     TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'approved',
  approved_at     TEXT NOT NULL,
  PRIMARY KEY (workspace_id, workflow_id, revision, policy_revision),
  CHECK (revision >= 1),
  CHECK (length(workspace_id) > 0),
  CHECK (length(workflow_id) > 0),
  CHECK (length(content_hash) = 64),
  CHECK (length(policy_revision) > 0),
  CHECK (length(approver_id) > 0)
);

CREATE INDEX IF NOT EXISTS idx_wab_lookup
  ON workflow_approval_bindings (workspace_id, workflow_id, revision, content_hash, policy_revision);

CREATE TRIGGER IF NOT EXISTS trg_wab_no_update
BEFORE UPDATE ON workflow_approval_bindings
BEGIN
  SELECT RAISE(ABORT, 'workflow_approval_bindings is append-only: UPDATE prohibited');
END;

CREATE TRIGGER IF NOT EXISTS trg_wab_no_delete
BEFORE DELETE ON workflow_approval_bindings
BEGIN
  SELECT RAISE(ABORT, 'workflow_approval_bindings is append-only: DELETE prohibited');
END;
