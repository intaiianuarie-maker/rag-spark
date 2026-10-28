"""SQLAlchemy model for per-workspace ModelConfig (TASK-M7-001a)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.models import Base


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="default")
    preferred_model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    override_model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    routing_rules: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    rate_limit_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="fail_closed"
    )
    verify_uniform_cost: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extra_models: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
