"""sync_source_documents table

Persisted identity for sync-produced documents: (workspace, connector,
external_id) -> document_id. The sync publisher/deleter resolve documents
through THIS mapping — never by guessing a filename — so nested external ids
delete correctly and two connectors with the same basename never overwrite
each other's documents (POLICE M6-001b REQUEST-CHANGES).

Identity is NEVER truncated (root review): the primary key carries a SHA-256
of the full external_id so distinct long paths sharing a prefix cannot alias
onto one row; the full id is stored verbatim as evidence.

Revision ID: 0016_sync_source_documents
Revises: 0015_connector_configs
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_sync_source_documents"
down_revision: str | None = "0015_connector_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_source_documents",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_id", sa.String(length=255), nullable=False),
        # SHA-256 of the FULL external id — the alias-proof identity (a
        # truncated id would let two long paths sharing a prefix collide).
        sa.Column("external_id_key", sa.String(length=64), nullable=False),
        # The full external id, verbatim (evidence; never a lookup key).
        sa.Column("external_id", sa.Text(), nullable=False),
        # Plain indexed column by design (no cross-package FK — the sync
        # progress rows follow the same pattern); the publisher/deleter keep
        # the pair consistent inside one transaction.
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "connector_id", "external_id_key", name="pk_sync_source_documents"
        ),
    )
    op.create_index(
        "ix_sync_source_documents_document_id",
        "sync_source_documents",
        ["document_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_sync_source_documents_document_id", table_name="sync_source_documents")
    op.drop_table("sync_source_documents")
