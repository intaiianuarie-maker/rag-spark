"""POST /ask — grounded answer with verified citations (TASK-M4-002).

The route is thin: authenticate + authorize (workspace read), build the answer
engine, and map failures. All retrieval/generation/verification logic lives in
`packages/answer` behind the orchestrator + LLM provider interfaces.

Carried from M4-001 acceptance: an `LLMProviderError` (gateway down) is
surfaced as **503**, never 500.
"""

from __future__ import annotations

import os
import re
import uuid

from answer.engine import AnswerEngine
from answer.factory import build_answer_engine
from answer.models import Answer
from core.auth import ACLScope, Principal
from fastapi import APIRouter, Depends, HTTPException, status
from models.adapters.litellm_provider import LLMProviderError
from pydantic import BaseModel, Field
from retrieval.interfaces import RetrievalMode

from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/ask", tags=["ask"])


class AskRequest(BaseModel):
    query: str = Field(min_length=1)
    model: str | None = None
    mode: RetrievalMode = RetrievalMode.HYBRID


def _http_error(exc: ApiError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    )


#: Code markers for query classification (fenced blocks or leading keywords).
_CODE_RE = re.compile(
    r"(```|^\s*(?:def |class |import |from .* import |function |const |let |"
    r"var |fn |public |private |SELECT |INSERT |UPDATE ))",
    re.IGNORECASE | re.MULTILINE,
)

#: RO/PT diacritics that never appear in plain English ASCII queries.
_MULTILINGUAL_RE = re.compile(r"[ăâșșțţãõçñéèêëíìîïóòôõúùûüýÿæœßøåäöü]", re.IGNORECASE)

#: Reasoning/complexity hint words (EN + RO + PT).
_COMPLEX_RE = re.compile(
    r"\b(?:why|how|compare|contrast|analyze|analyse|explain|evaluate|reason|plan|design|"
    r"de ce|cum|compară|explică|evaluează|analizează|por que|como|comparar|explicar|"
    r"avaliar|analisar)\b",
    re.IGNORECASE,
)


def classify_query(query: str) -> str:
    """Map a raw query to a real router query class.

    Returns one of ``"code"`` | ``"multilingual"`` | ``"complex"`` |
    ``"simple"`` — exactly the keys of
    :data:`models.router_config.REQUIRED_CAPABILITY`. Unknown/future classes
    are never returned: :func:`models.router_config.route` falls through to
    ``preferred_model`` for unknown classes (test-enshrined), so passing a
    placeholder such as ``"general"`` silently disables rule selection and
    makes the router call a no-op.

    Heuristic, disclosed limits: code fences/keywords win first, then RO/PT
    diacritics, then reasoning keywords or long queries (>12 words) map to
    ``"complex"``; everything else is ``"simple"``. This is a routing hint,
    not language identification — a full detector (e.g. lingua) is out of
    scope for v1 and would add a heavy dependency for no gate benefit.
    """
    if _CODE_RE.search(query):
        return "code"
    if _MULTILINGUAL_RE.search(query):
        return "multilingual"
    words = query.split()
    if len(words) > 12 or _COMPLEX_RE.search(query):
        return "complex"
    return "simple"


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

    from answer.factory import DEFAULT_ANSWER_MODEL
    from models.router_config import WorkspaceModelConfig, route

    # Honest router wiring (V1-COMPLETION fix order #4, M7-001a): classify the
    # query into a REAL router class so rule selection can engage, and honor
    # ANSWER_MODEL as the workspace preferred model (previously the route
    # always fell through to FALLBACK because "general" is not a known class
    # and preferred_model was never set, silently discarding ANSWER_MODEL).
    # Per-workspace routing_rules are not yet persisted (no DB table; adding
    # one requires an Alembic migration + coordinator decision), so with empty
    # rules the router still resolves to preferred/FALLBACK — the call is now
    # a real seam, not a no-op.
    query_class = classify_query(body.query)
    config = WorkspaceModelConfig(
        workspace_id=str(workspace_id),
        override_model=body.model,
        preferred_model=os.environ.get("ANSWER_MODEL", DEFAULT_ANSWER_MODEL),
    )
    resolved_model = route(query_class, config)

    engine: AnswerEngine = build_answer_engine()
    engine.model = resolved_model

    try:
        return await engine.answer(
            query=body.query,
            acl_scope=ACLScope(user_id=principal.user_id, workspace_ids=[str(workspace_id)]),
            mode=body.mode,
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
