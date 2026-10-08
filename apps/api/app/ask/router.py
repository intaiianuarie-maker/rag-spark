"""POST /ask — grounded answer with verified citations (TASK-M4-002).

The route is thin: authenticate + authorize (workspace read), build the answer
engine, and map failures. All retrieval/generation/verification logic lives in
`packages/answer` behind the orchestrator + LLM provider interfaces.

Carried from M4-001 acceptance: an `LLMProviderError` (gateway down) is
surfaced as **503**, never 500.
"""

from __future__ import annotations

import uuid

from answer.engine import AnswerEngine
from answer.factory import build_answer_engine
from answer.models import Answer
from core.auth import ACLScope, Principal
from fastapi import APIRouter, Depends, HTTPException, status
from models.adapters.litellm_provider import LLMProviderError
from pydantic import BaseModel, Field

from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/ask", tags=["ask"])


class AskRequest(BaseModel):
    query: str = Field(min_length=1)
    model: str | None = None


def _http_error(exc: ApiError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    )


@router.post(
    "",
    response_model=Answer,
    summary="Ask a question and get a grounded answer with citations",
)
async def ask(
    workspace_id: uuid.UUID,
    body: AskRequest,
    principal: Principal = Depends(get_principal),
) -> Answer:
    try:
        require(principal, Action.DOCUMENT_READ, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise _http_error(exc) from exc

    engine: AnswerEngine = build_answer_engine()
    if body.model:
        engine.model = body.model

    try:
        return await engine.answer(
            query=body.query,
            acl_scope=ACLScope(user_id=principal.user_id, workspace_ids=[str(workspace_id)]),
        )
    except LLMProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "llm_unavailable",
                "message": "The model gateway is unavailable; try again shortly.",
                "cause": str(exc)[:300],
            },
        ) from exc
