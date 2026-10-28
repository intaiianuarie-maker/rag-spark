"""Persist workspace evaluation datasets and runs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_evaluation_storage"
down_revision = "0013_sync_progress_del"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_datasets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evaluation_dataset_workspace"),
    )
    op.create_index("ix_evaluation_datasets_workspace_id", "evaluation_datasets", ["workspace_id"])
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dataset_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_id"],
            ["evaluation_datasets.workspace_id", "evaluation_datasets.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_evaluation_runs_workspace_id", "evaluation_runs", ["workspace_id"])
    op.create_index("ix_evaluation_runs_dataset_id", "evaluation_runs", ["dataset_id"])


def downgrade() -> None:
    op.drop_table("evaluation_runs")
    op.drop_table("evaluation_datasets")
