"""connector_sync_progress + connector_sync_outcomes (T-V1-034).

Revision ID: 0008_connector_sync_progress
Revises: 0007_lexical_docs
Create Date: 2026-10-13

T-V1-034 (resumable sync pipeline): progress must outlive a process, so the
consumer's persisted source identity, keyset cursor, snapshot digest, run
counters and per-item outcomes get their own tables instead of living in
memory. Two rules encoded here:

- progress is unique per SOURCE (workspace + connector) — one resumable cursor
  per configured source, upserted after every page;
- outcomes are unique per (source, generation, item) — a redelivered job
  upserts the same row rather than duplicating it (T-V1-020 at-least-once +
  T-V1-016 idempotent publication), and a reconfigured source starts a new
  generation so stale checksums can never classify new settings' items.

Hand-written like the rest of the chain (``alembic/env.py`` carries no
autogenerate metadata) and idempotent (``IF NOT EXISTS``), matching 0007.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_connector_sync_progress"
down_revision: str | None = "0007_lexical_docs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS connector_sync_progress (
            id UUID PRIMARY KEY,
            workspace_id VARCHAR(128) NOT NULL,
            connector_id VARCHAR(128) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            settings_fingerprint VARCHAR(64) NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1,
            cursor TEXT,
            snapshot VARCHAR(64),
            last_run_id VARCHAR(64),
            items_seen INTEGER NOT NULL DEFAULT 0,
            items_created INTEGER NOT NULL DEFAULT 0,
            items_updated INTEGER NOT NULL DEFAULT 0,
            items_unchanged INTEGER NOT NULL DEFAULT 0,
            items_failed INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_connector_sync_progress_source
                UNIQUE (workspace_id, connector_id)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_connector_sync_progress_workspace_id "
        "ON connector_sync_progress (workspace_id);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS connector_sync_outcomes (
            id UUID PRIMARY KEY,
            workspace_id VARCHAR(128) NOT NULL,
            connector_id VARCHAR(128) NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1,
            external_id TEXT NOT NULL,
            status VARCHAR(16) NOT NULL,
            checksum VARCHAR(64),
            size_bytes INTEGER,
            error_code VARCHAR(64),
            error_message TEXT,
            run_id VARCHAR(64),
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_connector_sync_outcomes_item
                UNIQUE (workspace_id, connector_id, generation, external_id)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_connector_sync_outcomes_workspace_id "
        "ON connector_sync_outcomes (workspace_id);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS connector_sync_outcomes;")
    op.execute("DROP TABLE IF EXISTS connector_sync_progress;")
