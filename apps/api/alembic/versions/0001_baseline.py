"""baseline (no domain tables)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-06

M0 baseline. The schema is intentionally empty: `docs/architecture/data-model.md`
mandates no domain tables until M1+. This revision exists so `alembic upgrade
head` is idempotent on boot and future revisions are diffable from a known head.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op baseline; domain tables (Organization/Workspace/...) land in M1+."""


def downgrade() -> None:
    """No-op baseline."""
