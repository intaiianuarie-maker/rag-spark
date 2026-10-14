"""Research lifecycle over HTTP — topic in, auditable report out (T-V3-022).

Stages: planning -> fan-out -> verify -> gaps -> contradictions -> synthesis.
Every stage gated identically to T-021 (budget/approval/workspace via same
get_principal + require + FREE-model-only). Report returns FULL honesty
package (grounded sentences with citations, named unknowns, contradiction
sides, abstention with disclosure). No content lost vs in-process.

Seam: create_research_pipeline() is the factory tests patch (like T-021's
create_runtime / search.create_orchestrator) so unit tests stage with fakes,
no live LLM. Free-model-only for any live generation (standing rule).

Storage: in-memory dict for skeleton (mirrors runtime _PERSISTED_*).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from agents.unavailable import (
    MISSING_RESEARCH_PIPELINE,
    UNAVAILABLE_CODE,
    Unavailable,
)
from agents.verifier import InMemoryChunkSource
from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.authz import Action, Resource, require
from app.authz.principal import get_principal

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/agent/research", tags=["research"])

_RESEARCH: dict[str, dict[str, Any]] = {}


def _clear_research_store() -> None:
    _RESEARCH.clear()


def _http_error(exc):
    from core.errors import error_detail

    from app.authz.errors import ApiError

    if isinstance(exc, ApiError):
        from fastapi import HTTPException

        return HTTPException(status_code=exc.status_code, detail=error_detail(exc.code, exc.message))  # noqa: E501
    raise exc from exc


def _require_workspace(principal: Principal, workspace_id: uuid.UUID) -> None:
    from app.authz.errors import ApiError

    try:
        require(principal, Action.DOCUMENT_READ, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise _http_error(exc) from exc


def create_research_pipeline():
    """Factory seam — tests monkeypatch this to stage with fakes.

    When an LLM provider is configured (via LiteLLM / OpenRouter surface),
    wires the real ResearchPipeline runner (T-R16).
    When unconfigured, returns None (preserving R02 unavailable contract).
    """
    from agents.factories import resolve_agent_llm_provider
    from agents.research_pipeline import create_research_pipeline_runner

    llm = resolve_agent_llm_provider(call_site="agents.research")
    if llm is None:
        return None
    return create_research_pipeline_runner(llm=llm)


class SubmitResearchRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=4096)
    max_questions: int = Field(default=3, ge=1, le=9)


class SubmitResearchResponse(BaseModel):
    research_id: str
    workspace_id: str
    status: str
    topic: str
    #: T-R02: set only when execution was refused as unavailable.
    error_code: str | None = None
    missing: str | None = None


class ResearchStatusResponse(BaseModel):
    research_id: str
    workspace_id: str
    status: str
    topic: str
    stage: str
    created_at: str


class ResearchReportResponse(BaseModel):
    research_id: str
    workspace_id: str
    status: str
    topic: str
    grounded_sentences: list[dict[str, Any]] = Field(default_factory=list)
    unknowns: list[dict[str, Any]] = Field(default_factory=list)
    contradictions: list[dict[str, Any]] = Field(default_factory=list)
    contradiction_outline: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    abstained: bool = False
    abstention_reason: str = ""
    searched: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    #: T-R02: typed unavailable contract, empty on a completed report.
    error_code: str | None = None
    missing: str | None = None


@router.post("", response_model=SubmitResearchResponse, status_code=status.HTTP_201_CREATED)
async def submit_research(
    workspace_id: uuid.UUID,
    body: SubmitResearchRequest,
    principal: Principal = Depends(get_principal),
) -> SubmitResearchResponse:
    _require_workspace(principal, workspace_id)

    # Free-model-only gate (standing rule) — body does not carry model, but
    # if pipeline were to use a model, it must be FREE; skeleton checks the
    # planner's FREE_MODELS seam so tests can prove paid -> BLOCKED.
    # For the skeleton we accept the topic and stage a minimal report with fakes.
    research_id = str(uuid.uuid4())
    created = datetime.now(UTC).isoformat()

    # Minimal staged report — mirrors in-process synthesis honesty package
    # Uses the same pipeline components as in-process, but with in-memory fakes
    # so the route mechanics are tested without live LLM.
    source = InMemoryChunkSource()
    topic = body.topic.strip()
    # If pipeline factory is patched, use it; otherwise there is no research
    # capability at all.
    pipeline = create_research_pipeline()
    if pipeline is None:
        # T-R02: no pipeline wired means NOTHING was researched. The previous
        # fallback invented chunk "chk-1" / evidence "ev-1", a grounded sentence
        # echoing the topic back, and unknowns/contradictions triggered purely by
        # the words "unknown"/"conflict" appearing in the topic — a report that
        # looked researched and cited evidence that was never retrieved. Return
        # the typed UNAVAILABLE instead: an empty honesty package naming the
        # missing piece, with nothing invented.
        report = Unavailable(
            MISSING_RESEARCH_PIPELINE,
            "No research pipeline is configured; nothing was searched, retrieved, "
            "generated or cited.",
        ).report(topic)
    else:
        from agents.policy import PolicyViolationError

        try:
            try:
                report = await pipeline(
                    topic,
                    str(workspace_id),
                    principal.user_id,
                    max_questions=body.max_questions,
                )
            except TypeError:
                report = await pipeline(topic, str(workspace_id), principal.user_id)
        except PolicyViolationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "policy_violation",
                    "message": str(exc),
                    "field": exc.field,
                    "requested": exc.requested,
                    "limit": exc.limit,
                },
            ) from exc
    _ = source  # retained: the wired pipeline path owns its own chunk source

    unavailable = report.get("error_code") == UNAVAILABLE_CODE
    status_value = "failed" if unavailable else "completed"
    stage = "unavailable" if unavailable else "synthesis"

    _RESEARCH[research_id] = {
        "research_id": research_id,
        "workspace_id": str(workspace_id),
        "principal_id": principal.user_id,
        "topic": topic,
        "status": status_value,
        "stage": stage,
        "created_at": created,
        "report": report,
    }

    return SubmitResearchResponse(
        research_id=research_id,
        workspace_id=str(workspace_id),
        status=status_value,
        topic=topic,
        error_code=str(rpt_error) if (rpt_error := report.get("error_code")) else None,
        missing=str(rpt_missing) if (rpt_missing := report.get("missing")) else None,
    )


@router.get("/{research_id}", response_model=ResearchStatusResponse)
async def get_research_status(
    workspace_id: uuid.UUID,
    research_id: str,
    principal: Principal = Depends(get_principal),
) -> ResearchStatusResponse:
    _require_workspace(principal, workspace_id)
    rec = _RESEARCH.get(research_id)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found", "message": "research not found"})  # noqa: E501
    if rec["workspace_id"] != str(workspace_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "workspace_mismatch", "message": "research belongs to a different workspace"})  # noqa: E501
    return ResearchStatusResponse(
        research_id=research_id,
        workspace_id=rec["workspace_id"],
        status=rec["status"],
        topic=rec["topic"],
        stage=rec["stage"],
        created_at=rec["created_at"],
    )


@router.get("/{research_id}/report", response_model=ResearchReportResponse)
async def get_research_report(
    workspace_id: uuid.UUID,
    research_id: str,
    principal: Principal = Depends(get_principal),
) -> ResearchReportResponse:
    _require_workspace(principal, workspace_id)
    rec = _RESEARCH.get(research_id)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found", "message": "research not found"})  # noqa: E501
    if rec["workspace_id"] != str(workspace_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "workspace_mismatch", "message": "research belongs to a different workspace"})  # noqa: E501
    rpt = rec["report"]
    return ResearchReportResponse(
        research_id=research_id,
        workspace_id=rec["workspace_id"],
        status=rec["status"],
        topic=rec["topic"],
        grounded_sentences=rpt.get("grounded_sentences", []),
        unknowns=rpt.get("unknowns", []),
        contradictions=rpt.get("contradictions", []),
        contradiction_outline=rpt.get("contradiction_outline", []),
        citations=rpt.get("citations", []),
        abstained=rpt.get("abstained", False),
        abstention_reason=rpt.get("abstention_reason", ""),
        searched=rpt.get("searched", []),
        limitations=rpt.get("limitations", []),
        error_code=rpt.get("error_code"),
        missing=rpt.get("missing"),
    )
