"""POST /ask — grounded answer with verified citations (TASK-M4-002).

The route is thin: authenticate + authorize (workspace read), resolve the
model through the policy router (T-V1-038: workspace → permitted override →
operator default, with the ordering owned by ``models.router_config.route``),
build the answer engine, and map failures. All retrieval/generation/verification
logic lives in `packages/answer` behind the orchestrator + LLM provider
interfaces.

Carried from M4-001 acceptance: an `LLMProviderError` (gateway down) is
surfaced as **503**, never 500.

T-V1-026 (honest failure statuses): both failure legs of this route carry a
``trace_id`` — the request's tracking ID — so a support ticket can be joined to
the retrieval/generation log lines. A required retrieval leg being down is a
**503** with the registered safe reason, exactly like the /search endpoint; a
genuinely empty corpus is still a normal ``insufficient_evidence`` answer.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import replace

from answer.engine import AnswerEngine
from answer.factory import build_answer_engine
from answer.models import Answer, AnswerStatus
from core.auth import ACLScope, Principal
from core.errors import ReasonCode, canonical_code, error_detail, spec_for
from core.logging import request_id_ctx
from fastapi import APIRouter, Depends, HTTPException, status
from models.adapters.litellm_provider import LLMProviderError
from models.router_config import WorkspaceModelConfig, route
from pydantic import BaseModel, Field
from retrieval.interfaces import RetrievalMode

from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/ask", tags=["ask"])

logger = logging.getLogger("knowledge_os.api.ask")


class AskRequest(BaseModel):
    query: str = Field(min_length=1)
    model: str | None = None
    mode: RetrievalMode = RetrievalMode.HYBRID


def _http_error(exc: ApiError) -> HTTPException:
    """Map an :class:`ApiError` onto the shared taxonomy envelope.

    T-V1-025: the ``{"error", "message"}`` shape is unchanged and the wire
    value comes from the canonical registry (``core.errors.error_detail``);
    unknown codes pass through verbatim, so no caller can be broken here.
    """
    return HTTPException(
        status_code=exc.status_code,
        detail=error_detail(exc.code, exc.message),
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


def load_model_config(workspace_id: str, *, override_model: str | None) -> WorkspaceModelConfig:
    """Authoritative model policy for ONE Ask request (T-V1-028 seam).

    Three tiers are handed to :func:`models.router_config.route`, which — and
    only which — decides their precedence:

    1. **workspace** — per-workspace ``routing_rules``. No rule store is
       persisted yet (a new table needs an Alembic migration + coordinator
       decision), so this tier currently contributes no rules and the seam is
       empty rather than fake: whatever a future loader returns here is passed
       through untouched, and the ordering stays policy-owned.
    2. **permitted override** — ``body.model`` supplied by the caller.
    3. **operator default** — ``ANSWER_MODEL`` when the operator configured it,
       else ``None`` so the policy module's own fallback applies. This file
       therefore contains **no model literal** and no default override of its
       own: the handler never picks a model, it only reports what the router
       picked.

    ``workspace_id`` is threaded through for the loader's use and for log
    correlation; it is unused while the workspace tier has no store.
    """
    return WorkspaceModelConfig(
        workspace_id=workspace_id,
        override_model=override_model,
        preferred_model=os.environ.get("ANSWER_MODEL") or None,
        routing_rules=(),
    )


def _rules_selected(config: WorkspaceModelConfig, query_class: str, model: str) -> bool:
    """True when the workspace-rule tier, not the default tier, produced `model`.

    Derived by asking the policy router again with only the rules stripped, so
    the recorded reason can never disagree with the policy's own precedence
    logic (no re-scoring is duplicated here). Disclosed limit: if rules were
    ever to select exactly the default-tier model, the reason records the
    default tier — the selection itself is still correct, and the rules tier is
    empty at this call site today.
    """
    if not config.routing_rules:
        return False
    return route(query_class, replace(config, routing_rules=())) != model


def selection_reason(config: WorkspaceModelConfig, query_class: str, model: str) -> str:
    """Label the precedence tier that produced `model`.

    Mirrors :func:`models.router_config.route` (veto → override → workspace
    rules → default) and returns a short enum string only: no query text, no
    prompt content, no credentials ever reach the log.
    """
    if config.sensitive:
        return "policy_veto_local_only"
    if config.override_model is not None:
        return "permitted_override"
    if _rules_selected(config, query_class, model):
        return "workspace_rules"
    if config.preferred_model is not None:
        return "operator_default"
    return "operator_default_unconfigured"


def resolve_model(
    workspace_id: str, *, query_class: str, override_model: str | None
) -> tuple[str, str]:
    """Resolve the model for one Ask request: ``(model, selection_reason)``.

    Every request on this path goes through here exactly once (T-V1-038):
    there is no second model source in the handler, so a request bypassing the
    router is structurally impossible rather than merely discouraged.
    """
    config = load_model_config(workspace_id, override_model=override_model)
    model = route(query_class, config)
    return model, selection_reason(config, query_class, model)


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

    # T-V1-038: one router call per request, no other model source. Classify
    # into a REAL router class so rule selection can engage (the old "general"
    # placeholder disabled it), then let route() own the precedence and record
    # which tier it used. The log line carries identifiers + reason only —
    # never the query text (no prompt content), never credentials.
    query_class = classify_query(body.query)
    resolved_model, reason = resolve_model(
        str(workspace_id), query_class=query_class, override_model=body.model
    )
    logger.info(
        "ask_model_selected",
        extra={
            "event": "ask_model_selected",
            "workspace_id": str(workspace_id),
            "query_class": query_class,
            "model": resolved_model,
            "reason": reason,
            "override_supplied": body.model is not None,
        },
    )

    engine: AnswerEngine = build_answer_engine()
    engine.model = resolved_model

    try:
        answer = await engine.answer(
            query=body.query,
            acl_scope=ACLScope(user_id=principal.user_id, workspace_ids=[str(workspace_id)]),
            mode=body.mode,
        )
    except LLMProviderError as exc:
        # T-V1-026: same tracking ID the engine's log line carries, so the
        # operator can join the API response to the retrieval/generation trace.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                **error_detail(
                    ReasonCode.LLM_UNAVAILABLE,
                    "The model gateway is unavailable; try again shortly.",
                ),
                "trace_id": request_id_ctx.get(),
                "cause": str(exc)[:300],
            },
        ) from exc

    if answer.status == AnswerStatus.UNAVAILABLE:
        # T-V1-026: a REQUIRED retrieval leg was down, so nothing was searched.
        # That is a service failure, not an answer — the same 503 + registered
        # safe reason + tracking ID the /search endpoint returns, so "no
        # evidence" and "retrieval is down" stay distinguishable everywhere.
        code = canonical_code(answer.reason_code or "") or ReasonCode.RETRIEVAL_UNAVAILABLE
        spec = spec_for(code)
        raise HTTPException(
            status_code=(spec.http_status if spec else None) or status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={**error_detail(code, answer.reason), "trace_id": answer.trace_id},
        )

    return answer
