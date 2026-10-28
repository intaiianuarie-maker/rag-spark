"""connector_configs table (M6-001c)"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_connector_configs"
down_revision = "0014_evaluation_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connector_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("credential_ref", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_connector_configs_workspace_connector", "connector_configs", ["workspace_id", "connector_id"]
    )
    op.create_index("ix_connector_configs_workspace_id", "connector_configs", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_connector_configs_workspace_id", table_name="connector_configs")
    op.drop_constraint("uq_connector_configs_workspace_connector", "connector_configs", type_="unique")
    op.drop_table("connector_configs")
