"""processing_generations table for processing-generation schema (T-OR-10).

Revision ID: 0011_processing_generations
Revises: 0010_drop_original_bytes
Create Date: 2026-10-16

T-OR-10: Processing-generation schema, candidate rebuild & atomic serving switch.

Tracks each generation of a collection build so a rebuild is always a
candidate first, verified, promoted atomically via the serving alias, and
rollable back. The serving path (``promote_replacement``) is one atomic alias
repoint; this table is the durable record of which generation is
``active`` / ``superseded`` / ``building`` / ``failed`` so the alias and the
row stay consistent and an operator can always name the rollback target.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_processing_generations"
down_revision: str | None = "0010_drop_original_bytes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create table if not exists (idempotent for existing runtime-DDL table)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS processing_generations (
            generation_id   VARCHAR NOT NULL,
            collection_name VARCHAR NOT NULL,
            serving_alias   VARCHAR NOT NULL,
            chunker_version VARCHAR NOT NULL,
            embedding_model VARCHAR NOT NULL,
            status          VARCHAR NOT NULL DEFAULT 'building'
                CHECK (status IN ('building', 'active', 'superseded', 'failed')),
            points_count    INTEGER NOT NULL DEFAULT 0,
            created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            promoted_at     TIMESTAMP WITH TIME ZONE,
            metadata        JSONB DEFAULT '{}'::jsonb,
            PRIMARY KEY (generation_id)
        );
        """
    )
    # A serving alias resolves to at most one active generation at a time.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_pg_serving_alias_active
        ON processing_generations (serving_alias)
        WHERE status = 'active';
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pg_collection ON processing_generations (collection_name);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pg_status ON processing_generations (status);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS processing_generations;")
