"""POST /v1/workspaces/{id}/eval/runs — run a golden dataset (TASK-M8-001).

Thin HTTP over the eval harness: authenticate + authorize (workspace read, same
ACL as ask/search), map the typed request to a golden dataset, run it through an
orchestrator-backed retriever (reuses the M3-004 hybrid retriever via the same
factory seam as `app/search`), and return the per-metric means.

The harness itself (`app.eval`) is retrieval-agnostic and hermetic; this route
only supplies a real retriever in production. Faithfulness uses the default
lexical proxy unless a scorer is configured server-side.
"""

from __future__ import annotations

import uuid

from core.auth import ACLScope, Principal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal
from app.eval import EvaluationDataset, RetrievalResult, evaluate_dataset
from app.eval.models import GoldenExample

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/eval", tags=["eval"])


def create_orchestrator():
    """Module seam over answer.factory.create_orchestrator (patchable).

    Function-local import keeps adapter clients lazy — only needed when an eval
    run executes, never at route import/startup.
    """
    from answer.factory import create_orchestrator as factory

    return factory()


def _make_retriever(orchestrator, acl_scope: ACLScope):
    """Build a retriever fn mapping a question -> RetrievalResult.

    Reuses the frozen hybrid orchestrator `retrieve()`; ranks chunks, collects
    their document ids (retrieved + cited), and joins their content as context.
    No new retrieval logic — same call the /search route makes.
    """

    async def retrieve(question: str) -> RetrievalResult:
        from retrieval.interfaces import RetrievalMode

        items = await orchestrator.retrieve(
            query=question,
            acl_scope=acl_scope,
            mode=RetrievalMode.HYBRID,
        )
        doc_ids = [item.document_id for item in items if item.document_id]
        context = "\n\n".join(item.content for item in items)
        return RetrievalResult(doc_ids=doc_ids, context=context)

    return retrieve


class GoldenExampleIn(BaseModel):
    question: str = Field(min_length=1)
    expected_answer: str = ""
    expected_sources: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class EvalRunRequest(BaseModel):
    name: str = "eval"
    examples: list[GoldenExampleIn] = Field(default_factory=list)


class EvalRunResponse(BaseModel):
    run_id: str
    dataset_id: str
    n_questions: int
    results: dict[str, float]


@router.post("/runs", response_model=EvalRunResponse, summary="Run a golden eval dataset")
async def run_eval(
    workspace_id: uuid.UUID,
    body: EvalRunRequest,
    principal: Principal = Depends(get_principal),
) -> EvalRunResponse:
    try:
        require(principal, Action.DOCUMENT_READ, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.code, "message": exc.message},
        ) from exc

    dataset = EvaluationDataset(
        id=f"ds-{uuid.uuid4().hex[:12]}",
        workspace_id=str(workspace_id),
        name=body.name,
        examples=[
            GoldenExample(
                question=e.question,
                expected_answer=e.expected_answer,
                expected_sources=e.expected_sources,
                tags=e.tags,
            )
            for e in body.examples
        ],
    )

    orchestrator = create_orchestrator()
    acl_scope = ACLScope(user_id=principal.user_id, workspace_ids=[str(workspace_id)])
    retrieve = _make_retriever(orchestrator, acl_scope)

    run = await evaluate_dataset(dataset, retrieve)

    n = int(run.config_snapshot.get("n_questions", 0))
    return EvalRunResponse(
        run_id=run.id,
        dataset_id=run.dataset_id,
        n_questions=n,
        results=run.results,
    )
