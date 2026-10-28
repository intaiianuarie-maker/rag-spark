"""Pydantic schemas for ModelConfig CRUD (TASK-M7-001a)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RoutingRuleIn(BaseModel):
    dimension: str
    value: str
    priority: int = 0


class ModelConfigCreate(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=120)
    preferred_model: str | None = Field(default=None, max_length=256)
    override_model: str | None = Field(default=None, max_length=256)
    routing_rules: list[RoutingRuleIn] | None = None
    rate_limit_policy: str = Field(default="fail_closed", max_length=32)
    verify_uniform_cost: bool = True
    sensitive: bool = False
    extra_models: list[dict[str, Any]] | None = None


class ModelConfigUpdate(BaseModel):
    preferred_model: str | None = Field(default=None, max_length=256)
    override_model: str | None = Field(default=None, max_length=256)
    routing_rules: list[RoutingRuleIn] | None = None
    rate_limit_policy: str | None = Field(default=None, max_length=32)
    verify_uniform_cost: bool | None = None
    sensitive: bool | None = None
    extra_models: list[dict[str, Any]] | None = None


class ModelConfigPublic(BaseModel):
    id: str
    workspace_id: str
    name: str
    preferred_model: str | None = Field(default=None, max_length=256)
    override_model: str | None = Field(default=None, max_length=256)
    routing_rules: list[dict[str, Any]] | None = None
    rate_limit_policy: str = Field(max_length=32)
    verify_uniform_cost: bool
    sensitive: bool
    extra_models: list[dict[str, Any]] | None = None
    created_at: datetime
    updated_at: datetime
