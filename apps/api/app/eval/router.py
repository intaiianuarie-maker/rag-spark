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
from time import perf_counter

from core.auth import ACLScope, Principal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal
from app.eval import EvaluationDataset, RetrievalResult, evaluate_dataset
from app.eval.models import GoldenExample
from app.eval.storage import EvaluationStore

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/eval", tags=["eval"])
legacy_router = APIRouter(prefix="/v1/evaluations", tags=["eval"])


def create_orchestrator(*, sensitive: bool = False):
    """Module seam over answer.factory.create_orchestrator (patchable).

    Function-local import keeps adapter clients lazy — only needed when an eval
    run executes, never at route import/startup.
    """
    from answer.factory import create_orchestrator as factory

    return factory(sensitive=sensitive)


def _make_retriever(orchestrator, acl_scope: ACLScope, config: RetrievalConfig | None = None):
    """Build a retriever fn mapping a question -> RetrievalResult.

    Reuses the frozen hybrid orchestrator `retrieve()`; ranks chunks, collects
    their document ids (retrieved + cited), and joins their content as context.
    No new retrieval logic — same call the /search route makes.
    """

    async def retrieve(question: str) -> RetrievalResult:
        from retrieval.interfaces import RetrievalMode

        started = perf_counter()
        options = config or RetrievalConfig()
        items = await orchestrator.retrieve(
            query=question,
            acl_scope=acl_scope,
            mode=RetrievalMode(options.mode),
            context_top_k=options.top_k,
        )
        doc_ids = [item.document_id for item in items if item.document_id]
        context = "\n\n".join(item.content for item in items)
        # Retrieval-only evaluation makes no generation calls. Generation token
        # and cost accounting are therefore zero, not estimates of model spend.
        return RetrievalResult(
            doc_ids=doc_ids,
            context=context,
            latency_ms=(perf_counter() - started) * 1000,
        )

    return retrieve


class GoldenExampleIn(BaseModel):
    question: str = Field(min_length=1)
    expected_answer: str = ""
    expected_sources: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class RetrievalConfig(BaseModel):
    mode: str = Field(default="hybrid", pattern="^(hybrid|semantic|keyword)$")
    top_k: int = Field(default=10, ge=1, le=100)
    reranker: bool = True


class EvalRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "eval"
    examples: list[GoldenExampleIn] = Field(default_factory=list)
    dataset_id: str | None = None
    config: RetrievalConfig = Field(default_factory=RetrievalConfig)
    traces: list[TraceInput] | None = None
    experiment: dict[str, str | int | bool] = Field(default_factory=dict)


class DatasetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="eval", min_length=1, max_length=120)
    examples: list[GoldenExampleIn] = Field(min_length=1)


class TraceInput(BaseModel):
    """Explicit imported evaluation trace; text is used transiently only."""

    doc_ids: list[str] = Field(default_factory=list)
    cited_doc_ids: list[str] | None = None
    context: str = ""
    answer: str = ""
    cost: float = Field(default=0, ge=0, allow_inf_nan=False)
    latency_ms: float = Field(default=0, ge=0, allow_inf_nan=False)
    tokens: int = Field(default=0, ge=0)


class EvalRunResponse(BaseModel):
    run_id: str
    dataset_id: str
    n_questions: int
    results: dict[str, float]
    cost: float = 0.0
    latency_ms: float = 0.0
    tokens: int = 0
    config_snapshot: dict = Field(default_factory=dict)
    accounting_complete: bool = False


def _authorize(workspace_id: uuid.UUID, principal: Principal) -> None:
    try:
        require(principal, Action.DOCUMENT_READ, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}
        ) from exc


def _response(run) -> EvalRunResponse:
    return EvalRunResponse(
        run_id=run.id,
        dataset_id=run.dataset_id,
        n_questions=int(run.config_snapshot.get("n_questions", 0)),
        results=run.results,
        cost=run.cost,
        latency_ms=run.latency_ms,
        tokens=run.tokens,
        config_snapshot=run.config_snapshot,
        accounting_complete=run.accounting_complete,
    )


@router.post("/datasets", response_model=EvaluationDataset, status_code=201)
async def create_dataset(
    workspace_id: uuid.UUID,
    body: DatasetCreateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> EvaluationDataset:
    _authorize(workspace_id, principal)
    dataset = EvaluationDataset(
        id=f"ds-{uuid.uuid4().hex}",
        workspace_id=str(workspace_id),
        name=body.name,
        examples=[GoldenExample.model_validate(e.model_dump()) for e in body.examples],
    )
    await EvaluationStore(session).save_dataset(workspace_id, dataset)
    return dataset


@router.get("/datasets", response_model=list[EvaluationDataset])
async def list_datasets(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[EvaluationDataset]:
    _authorize(workspace_id, principal)
    return await EvaluationStore(session).list_datasets(workspace_id)


@router.get("/datasets/{dataset_id}", response_model=EvaluationDataset)
async def get_dataset(
    workspace_id: uuid.UUID,
    dataset_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> EvaluationDataset:
    _authorize(workspace_id, principal)
    dataset = await EvaluationStore(session).get_dataset(workspace_id, dataset_id)
    if dataset is None:
        raise HTTPException(404, detail="dataset not found")
    return dataset


@router.get("/runs", response_model=list[EvalRunResponse])
async def list_runs(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[EvalRunResponse]:
    _authorize(workspace_id, principal)
    return [_response(run) for run in await EvaluationStore(session).list_runs(workspace_id)]


@router.get("/runs/{run_id}", response_model=EvalRunResponse)
async def get_run(
    workspace_id: uuid.UUID,
    run_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> EvalRunResponse:
    _authorize(workspace_id, principal)
    run = await EvaluationStore(session).get_run(workspace_id, run_id)
    if run is None:
        raise HTTPException(404, detail="run not found")
    return _response(run)


@router.post("/runs", response_model=EvalRunResponse, summary="Run a golden eval dataset")
async def run_eval(
    workspace_id: uuid.UUID,
    body: EvalRunRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> EvalRunResponse:
    try:
        require(principal, Action.DOCUMENT_READ, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.code, "message": exc.message},
        ) from exc

    store = EvaluationStore(session)
    if body.dataset_id is not None and body.examples:
        raise HTTPException(422, detail="supply dataset_id or examples, not both")
    dataset = await store.get_dataset(workspace_id, body.dataset_id) if body.dataset_id else None
    if body.dataset_id and dataset is None:
        raise HTTPException(404, detail="dataset not found")
    if dataset is None:
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
        await store.save_dataset(workspace_id, dataset)

    if not dataset.examples:
        raise HTTPException(422, detail="evaluation requires at least one golden example")
    experiment_keys = {"embedder", "chunker", "retriever", "model", "prompt"}
    if set(body.experiment) - experiment_keys:
        raise HTTPException(422, detail="unsupported experiment configuration dimension")
    if body.experiment and body.traces is None:
        raise HTTPException(
            422, detail="external pipeline experiment settings require imported traces"
        )

    from models.router_config import load_workspace_config as _load_workspace_config

    _eval_workspace = _load_workspace_config(str(workspace_id))
    _eval_sensitive = _eval_workspace.sensitive if _eval_workspace is not None else False
    acl_scope = ACLScope(user_id=principal.user_id, workspace_ids=[str(workspace_id)])
    if body.traces is not None:
        if len(body.traces) != len(dataset.examples):
            raise HTTPException(
                422, detail="one trace required per golden example, in dataset order"
            )
        traces = iter(body.traces)

        def retrieve_trace(_question: str) -> RetrievalResult:
            trace = next(traces)
            return RetrievalResult(
                **trace.model_dump(),
                accounting_complete={"cost", "tokens", "latency_ms"}.issubset(
                    trace.model_fields_set
                ),
            )

        retrieve = retrieve_trace
    else:
        orchestrator = create_orchestrator(sensitive=_eval_sensitive)
        orchestrator.rerank_enabled = body.config.reranker
        retrieve = _make_retriever(orchestrator, acl_scope, body.config)

    run = await evaluate_dataset(
        dataset,
        retrieve,
        config_snapshot={
            "retrieval": body.config.model_dump(),
            "sensitive": _eval_sensitive,
            "experiment": body.experiment,
            "accounting": "imported_trace"
            if body.traces is not None
            else "retrieval_usage_unreported",
        },
    )
    await store.save_run(workspace_id, run)

    return _response(run)


@router.post("/runs/{run_id}/replay", response_model=EvalRunResponse)
async def replay_run(
    workspace_id: uuid.UUID,
    run_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> EvalRunResponse:
    from app.eval.runner import replay_snapshot

    _authorize(workspace_id, principal)
    store = EvaluationStore(session)
    original = await store.get_run(workspace_id, run_id)
    if original is None:
        raise HTTPException(404, detail="run not found")
    if "replay_inputs" not in original.config_snapshot:
        raise HTTPException(422, detail="run predates replay snapshots")
    run = await replay_snapshot(original.config_snapshot)
    run.config_snapshot["replay_of"] = original.id
    await store.save_run(workspace_id, run)
    return _response(run)


class LegacyRunRequest(EvalRunRequest):
    workspace_id: uuid.UUID


class ComparisonRequest(BaseModel):
    baseline_run_id: str
    candidate_run_id: str
    dimension: str


@router.post("/comparisons")
async def compare_saved_runs(
    workspace_id: uuid.UUID,
    body: ComparisonRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
):
    from app.eval.compare import compare_runs

    _authorize(workspace_id, principal)
    store = EvaluationStore(session)
    baseline = await store.get_run(workspace_id, body.baseline_run_id)
    candidate = await store.get_run(workspace_id, body.candidate_run_id)
    if baseline is None or candidate is None:
        raise HTTPException(404, detail="run not found")
    try:
        return compare_runs(baseline, candidate, dimension=body.dimension)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@legacy_router.post("/run", response_model=EvalRunResponse)
async def legacy_run_eval(
    body: LegacyRunRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> EvalRunResponse:
    return await run_eval(body.workspace_id, body, principal, session)


@legacy_router.get("/{run_id}", response_model=EvalRunResponse)
async def legacy_get_run(
    run_id: str,
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> EvalRunResponse:
    return await get_run(workspace_id, run_id, principal, session)
