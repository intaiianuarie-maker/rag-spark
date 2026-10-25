"""model_configs table

Revision ID: 0012_model_configs
Revises: 0011_processing_generations
Create Date: 2026-10-25

TASK-M7-001a: per-workspace ModelConfig persistence.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_model_configs"
down_revision: str | None = "0011_processing_generations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=120), nullable=False, server_default="default"),
        sa.Column("preferred_model", sa.String(length=256), nullable=True),
        sa.Column("override_model", sa.String(length=256), nullable=True),
        sa.Column("routing_rules", sa.JSON(), nullable=True),
        sa.Column("rate_limit_policy", sa.String(length=32), nullable=False, server_default="fail_closed"),
        sa.Column("verify_uniform_cost", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("extra_models", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_model_configs_workspace_id", "model_configs", ["workspace_id"])
    op.create_index("ix_model_configs_created_by", "model_configs", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_model_configs_created_by", table_name="model_configs")
    op.drop_index("ix_model_configs_workspace_id", table_name="model_configs")
    op.drop_table("model_configs")
