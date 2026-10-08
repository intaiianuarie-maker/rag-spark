"""lexical_docs table for PG FTS (TASK-M3-003).

Revision ID: 0007_lexical_docs
Revises: 0006_dedup_tombstones
Create Date: 2026-10-08

M3-003: tsvector/GIN lexical store. Replaces runtime DDL (AGENTS rule).
Includes page/section/tsv_cfg columns for Majors 5/6.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_lexical_docs"
down_revision: str | None = "0006_dedup_tombstones"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create table if not exists (idempotent for existing runtime-DDL table)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS lexical_docs (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            document_id TEXT,
            document_version_id TEXT,
            chunk_id TEXT,
            content TEXT NOT NULL,
            language TEXT,
            metadata JSONB DEFAULT '{}'::jsonb,
            tsv tsvector,
            tsv_cfg TEXT,
            page INTEGER,
            section TEXT
        );
        """
    )
    # Add columns if table existed from runtime DDL without them
    op.execute("ALTER TABLE lexical_docs ADD COLUMN IF NOT EXISTS tsv_cfg TEXT;")
    op.execute("ALTER TABLE lexical_docs ADD COLUMN IF NOT EXISTS page INTEGER;")
    op.execute("ALTER TABLE lexical_docs ADD COLUMN IF NOT EXISTS section TEXT;")
    op.execute("ALTER TABLE lexical_docs ADD COLUMN IF NOT EXISTS tsv tsvector;")
    op.execute("CREATE INDEX IF NOT EXISTS ix_lexical_docs_workspace ON lexical_docs (workspace_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_lexical_docs_tsv ON lexical_docs USING GIN (tsv);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lexical_docs;")
