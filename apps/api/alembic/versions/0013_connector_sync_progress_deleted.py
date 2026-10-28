"""connector sync progress: deletion + last attempt/success (M6-001b)"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_sync_progress_del"
down_revision: str | None = "0012_model_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connector_sync_progress",
        sa.Column("items_deleted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "connector_sync_progress",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connector_sync_progress",
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("connector_sync_progress", "last_success_at")
    op.drop_column("connector_sync_progress", "last_attempt_at")
    op.drop_column("connector_sync_progress", "items_deleted")
