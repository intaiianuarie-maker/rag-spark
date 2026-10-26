"""sync progress failure markers

Columns for the durable source-level failure state (root review): a discovery
failure (unreachable host, bad credentials) happens BEFORE any ItemOutcome, so
without these the status endpoint could not distinguish "attempting" from
"source is failing" and clients polled forever.

Revision ID: 0017_sync_failure_markers
Revises: 0016_sync_source_documents
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_sync_failure_markers"
down_revision: str | None = "0016_sync_source_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connector_sync_progress",
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "connector_sync_progress",
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("connector_sync_progress", "last_finished_at")
    op.drop_column("connector_sync_progress", "last_error_code")
