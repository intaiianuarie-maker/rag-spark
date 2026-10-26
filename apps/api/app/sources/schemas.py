"""Public schemas for workspace connector configuration."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from connectors.config_validation import (
    SETTING_MODELS as SETTING_MODELS,
)
from connectors.config_validation import (
    GitSettings as GitSettings,
)
from connectors.config_validation import (
    LocalSettings as LocalSettings,
)
from connectors.config_validation import (
    S3Settings as S3Settings,
)
from connectors.config_validation import (
    WebSettings as WebSettings,
)
from pydantic import BaseModel, ConfigDict, Field


class ConnectorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    kind: Literal["local_upload", "web", "s3", "git"]
    settings: dict[str, Any] = Field(default_factory=dict)
    credential_ref: str | None = Field(default=None, max_length=256)


class ConnectorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: dict[str, Any] | None = None
    credential_ref: str | None = None


class ConnectorPublic(BaseModel):
    connector_id: str
    kind: str
    settings: dict[str, Any]
    credential_ref: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class SyncTriggerResponse(BaseModel):
    job_id: str
    workspace_id: str
    connector_id: str | None = None
    acked: bool = False


class SyncStatusResponse(BaseModel):
    connector_id: str
    kind: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_run_id: str | None = None
    last_finished_at: datetime | None = None
    state: Literal["idle", "running", "succeeded", "failed"] = "idle"
    items_seen: int = 0
    items_created: int = 0
    items_updated: int = 0
    items_unchanged: int = 0
    items_failed: int = 0
    items_deleted: int = 0
    cursor: str | None = None
