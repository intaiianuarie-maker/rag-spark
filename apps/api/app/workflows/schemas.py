"""Schemas and contracts for V4 Workflow API (T-R19).

Pure request and response models for the 8 authorized V4 handlers:
1. draft
2. import
3. validate
4. save
5. approve
6. run
7. status
8. cancel
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from workflow_engine.interfaces import (
    ValidationError,
    ValidationResult,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowRun,
    WorkflowRunStatus,
)
from workflow_engine.routes.schemas import (
    ApiErrorBody,
    ApiErrorDetail,
    DraftErrorCode,
    DraftHistoryQuery,
    DraftHistoryResponse,
    DraftMutationResponse,
    DraftReviseRequest,
    DraftRevisionDetail,
    DraftRevisionMeta,
    DraftSaveRequest,
    assert_path_scope_matches,
)
from workflow_engine.view_models import EditorAnnotation, RunTimeline


class WorkflowImportRequest(BaseModel):
    """Payload to import a workflow from Langflow export JSON or native format."""

    model_config = ConfigDict(extra="forbid")

    flow_data: dict[str, Any]
    workflow_id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4096)
    save: bool = Field(default=True, description="Whether to store immediately as revision 1")


class WorkflowValidateRequest(BaseModel):
    """Payload to validate a workflow definition or stored draft."""

    model_config = ConfigDict(extra="forbid")

    definition: WorkflowDefinition | None = None
    workflow_id: str | None = Field(default=None, max_length=128)
    revision: int | None = Field(default=None, ge=1)


class WorkflowValidateResponse(BaseModel):
    """Structural and node validation result with placeable editor annotations."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[ValidationError] = Field(default_factory=list)
    annotations: list[EditorAnnotation] = Field(default_factory=list)


class WorkflowApproveRequest(BaseModel):
    """Approval request for a specific workflow revision."""

    model_config = ConfigDict(extra="forbid")

    revision: int | None = Field(default=None, ge=1)
    comment: str = Field(default="", max_length=1024)


class WorkflowApproveResponse(BaseModel):
    """Approval record confirming the revision is vetted and authorized for execution."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    workflow_id: str
    workspace_id: str
    revision: int
    content_hash: str
    approved_by: str
    approved_at: str
    comment: str = ""


class WorkflowRunRequest(BaseModel):
    """Request to execute a workflow revision."""

    model_config = ConfigDict(extra="forbid")

    revision: int | None = Field(default=None, ge=1)
    inputs: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunResponse(BaseModel):
    """Run details, execution state, and terminal timeline model."""

    model_config = ConfigDict(extra="forbid")

    run: WorkflowRun
    timeline: RunTimeline


class DraftListResponse(BaseModel):
    """List of all workflow drafts present in a workspace."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    count: int
    drafts: list[DraftRevisionMeta] = Field(default_factory=list)


__all__ = [
    "ApiErrorBody",
    "ApiErrorDetail",
    "DraftErrorCode",
    "DraftHistoryQuery",
    "DraftHistoryResponse",
    "DraftListResponse",
    "DraftMutationResponse",
    "DraftReviseRequest",
    "DraftRevisionDetail",
    "DraftRevisionMeta",
    "DraftSaveRequest",
    "EditorAnnotation",
    "RunTimeline",
    "ValidationError",
    "ValidationResult",
    "WorkflowApproveRequest",
    "WorkflowApproveResponse",
    "WorkflowDefinition",
    "WorkflowImportRequest",
    "WorkflowRevision",
    "WorkflowRun",
    "WorkflowRunRequest",
    "WorkflowRunResponse",
    "WorkflowRunStatus",
    "WorkflowValidateRequest",
    "WorkflowValidateResponse",
    "assert_path_scope_matches",
]
