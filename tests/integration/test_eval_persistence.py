"""Real PostgreSQL persistence in an isolated schema; never migrates public."""

from __future__ import annotations

import hashlib
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.auth.db import get_session
from app.authz.principal import get_principal
from app.eval import EvaluationDataset, RetrievalResult, evaluate_dataset
from app.eval.router import legacy_router, router
from core.auth import Principal, Role
from core.config import get_settings
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_eval_persists_reloads_scopes_and_reproduces(monkeypatch):
    """HTTP → real runner → DB → fresh sessions; fixture bytes stay intact."""
    schema = "eval_test_" + uuid.uuid4().hex
    url = get_settings().database_url
    admin = create_async_engine(url, connect_args={"connect_timeout": 3})
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    ws, other = uuid.uuid4(), uuid.uuid4()
    fixture_files = sorted((Path(__file__).parents[2] / "evals").glob("*.jsonl"))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in fixture_files}
    try:
        async with admin.connect():
            pass
    except OperationalError:
        await engine.dispose()
        await admin.dispose()
        pytest.skip("PostgreSQL unavailable; start compose postgres for isolated-schema eval tests")
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE workspaces (id uuid PRIMARY KEY)"))
            await conn.execute(
                text("INSERT INTO workspaces (id) VALUES (:a), (:b)"), {"a": ws, "b": other}
            )
            migration_path = (
                Path(__file__).parents[2] / "apps/api/alembic/versions/0014_evaluation_storage.py"
            )
            spec = importlib.util.spec_from_file_location("eval_storage_migration", migration_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            def migrate(sync_conn):
                module.op = Operations(MigrationContext.configure(sync_conn))
                module.upgrade()

            await conn.run_sync(migrate)

        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def session_dependency():
            async with sessions() as session:
                yield session

        principal = Principal(
            user_id="fixture-user",
            is_authenticated=True,
            workspace_roles={str(ws): Role.OWNER, str(other): Role.OWNER},
        )
        seen_scopes = []

        class FixtureOrchestrator:
            async def retrieve(self, *, query, acl_scope, mode, context_top_k):
                seen_scopes.append(acl_scope.workspace_ids)
                assert mode.value == "hybrid" and context_top_k == 2
                ids = ["a"] if query == "q1" else ["b", "noise"]
                return [SimpleNamespace(document_id=d, content="fixture context") for d in ids]

        monkeypatch.setattr(
            "app.eval.router.create_orchestrator", lambda **_: FixtureOrchestrator()
        )
        app = FastAPI()
        app.include_router(router)
        app.include_router(legacy_router)
        app.dependency_overrides[get_session] = session_dependency
        app.dependency_overrides[get_principal] = lambda: principal
        prefix = f"/v1/workspaces/{ws}/eval"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = {
                "name": "fixed corpus",
                "examples": [
                    {"question": "q1", "expected_sources": ["a"]},
                    {"question": "q2", "expected_sources": ["b", "c"]},
                ],
            }
            created = await client.post(prefix + "/datasets", json=body)
            assert created.status_code == 201, created.text
            dataset_id = created.json()["id"]
            run_response = await client.post(
                prefix + "/runs",
                json={
                    "dataset_id": dataset_id,
                    "config": {"top_k": 2},
                },
            )
            assert run_response.status_code == 200, run_response.text
            run = run_response.json()
            assert run["results"]["recall"] == 0.75
            assert run["results"]["precision"] == 0.75
            assert run["results"]["mrr"] == 1.0
            assert run["latency_ms"] > 0
            assert run["cost"] == 0 and run["tokens"] == 0  # fixture makes no model calls
            assert seen_scopes == [[str(ws)], [str(ws)]]
            # Every HTTP request opens a new database session. Disposal also
            # discards pooled connections, proving there is no in-memory store.
            await engine.dispose()
            loaded = await client.get(prefix + "/runs/" + run["run_id"])
            assert loaded.json() == run
            datasets = await client.get(prefix + "/datasets")
            assert datasets.json()[0]["id"] == dataset_id
            runs = await client.get(prefix + "/runs")
            assert runs.json() == [run]
            for suffix in ("/runs/" + run["run_id"], "/datasets/" + dataset_id):
                foreign = await client.get(f"/v1/workspaces/{other}/eval" + suffix)
                assert foreign.status_code == 404
            denied = await client.get(f"/v1/workspaces/{uuid.uuid4()}/eval/runs")
            assert denied.status_code == 403
            rerun = await client.post(
                prefix + "/runs", json={"dataset_id": dataset_id, "config": {"top_k": 2}}
            )
            assert rerun.json()["results"] == run["results"]
            snapshot = run["config_snapshot"]
            # Reconstruct the exact golden inputs from the snapshot alone.
            dataset = EvaluationDataset.model_validate(snapshot["dataset"])
            replay = await evaluate_dataset(
                dataset,
                lambda q: RetrievalResult(
                    doc_ids=["a"] if q == "q1" else ["b", "noise"], context="fixture context"
                ),
                config_snapshot={"retrieval": snapshot["retrieval"]},
            )
            assert replay.results == run["results"]
            assert replay.config_snapshot["dataset_sha256"] == snapshot["dataset_sha256"]
            assert "fixture context" not in str(run)  # no retrieved text stored
            imported = await client.post(
                "/v1/evaluations/run",
                json={
                    "workspace_id": str(ws),
                    "dataset_id": dataset_id,
                    "traces": [
                        {
                            "doc_ids": ["a"],
                            "tokens": 7,
                            "cost": 0.02,
                            "latency_ms": 20,
                            "answer": "private answer",
                            "context": "private source text",
                        },
                        {"doc_ids": ["b", "noise"], "tokens": 11, "cost": 0.03, "latency_ms": 30},
                    ],
                },
            )
            assert imported.status_code == 200, imported.text
            scored = imported.json()
            assert scored["tokens"] == 18
            assert scored["cost"] == pytest.approx(0.05)
            assert scored["latency_ms"] == 50
            assert scored["accounting_complete"] is True
            assert scored["results"]["recall"] == 0.75
            assert "private answer" not in str(scored)
            assert "private source text" not in str(scored)
            readback = await client.get(
                "/v1/evaluations/" + scored["run_id"], params={"workspace_id": str(ws)}
            )
            assert readback.json() == scored
            replay_response = await client.post(prefix + "/runs/" + scored["run_id"] + "/replay")
            assert replay_response.status_code == 200, replay_response.text
            replayed = replay_response.json()
            assert replayed["run_id"] != scored["run_id"]
            for field in ("results", "cost", "tokens", "latency_ms", "accounting_complete"):
                assert replayed[field] == scored[field]
            assert replayed["config_snapshot"]["replay_of"] == scored["run_id"]
            invalid_trace = await client.post(
                prefix + "/runs",
                json={
                    "dataset_id": dataset_id,
                    "traces": [{"cost": -1}],
                },
            )
            assert invalid_trace.status_code == 422
            partial_usage = await client.post(
                prefix + "/runs",
                json={
                    "dataset_id": dataset_id,
                    "traces": [{"doc_ids": ["a"]}, {"doc_ids": ["b"]}],
                },
            )
            assert partial_usage.status_code == 200
            assert partial_usage.json()["accounting_complete"] is False
            assert (await client.post(prefix + "/runs", json={"examples": []})).status_code == 422
            assert (
                await client.post(prefix + "/datasets", json={"examples": []})
            ).status_code == 422
            assert (
                await client.post(prefix + "/datasets", json={**body, "traces": []})
            ).status_code == 422
            # Two full, saved fixture runs differing only in reranking.
            base_response = await client.post(
                prefix + "/runs",
                json={
                    "dataset_id": dataset_id,
                    "config": {"top_k": 2, "reranker": False},
                    "traces": [
                        {"doc_ids": ["noise", "a"], "cost": 0.03, "tokens": 10, "latency_ms": 40},
                        {"doc_ids": ["noise", "b"], "cost": 0.03, "tokens": 10, "latency_ms": 40},
                    ],
                },
            )
            candidate_response = await client.post(
                prefix + "/runs",
                json={
                    "dataset_id": dataset_id,
                    "config": {"top_k": 2, "reranker": True},
                    "traces": [
                        {"doc_ids": ["a", "noise"], "cost": 0.02, "tokens": 8, "latency_ms": 30},
                        {"doc_ids": ["b", "noise"], "cost": 0.02, "tokens": 8, "latency_ms": 30},
                    ],
                },
            )
            assert base_response.status_code == candidate_response.status_code == 200
            pair = {
                "baseline_run_id": base_response.json()["run_id"],
                "candidate_run_id": candidate_response.json()["run_id"],
                "dimension": "reranker",
            }
            compared = await client.post(prefix + "/comparisons", json=pair)
            assert compared.status_code == 200, compared.text
            comparison = compared.json()
            assert comparison["configuration_validated"] is True
            deltas = {row["metric"]: row for row in comparison["metric_deltas"]}
            assert deltas["mrr"]["delta"] == 0.5
            assert deltas["mrr"]["direction"] == "improved"
            assert deltas["recall"]["delta"] == 0
            assert comparison["cost_delta"] == pytest.approx(-0.02)
            assert comparison["latency_delta_ms"] == -20
            assert comparison["tokens_delta"] == -4
            wrong_dimension = await client.post(
                prefix + "/comparisons", json={**pair, "dimension": "top_k"}
            )
            assert wrong_dimension.status_code == 422
            foreign_pair = await client.post(f"/v1/workspaces/{other}/eval/comparisons", json=pair)
            assert foreign_pair.status_code == 404
        assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in fixture_files}
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()
