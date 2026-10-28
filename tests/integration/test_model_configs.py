"""ModelConfig HTTP/SQL persistence against an isolated PostgreSQL schema."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.auth.db import get_session
from app.auth.workspaces import models as workspace_models  # noqa: F401 - registers FK targets
from app.authz.principal import get_principal
from app.models_config.router import router
from core.auth import Principal, Role
from core.config import get_settings
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_model_config_crud_isolation_and_restart() -> None:
    schema = "modelcfg_test_" + uuid.uuid4().hex
    url = get_settings().database_url
    admin = create_async_engine(url, connect_args={"connect_timeout": 3})
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        async with admin.connect():
            pass
    except OperationalError:
        await engine.dispose()
        await admin.dispose()
        pytest.skip("PostgreSQL unavailable; start compose postgres for isolated ModelConfig tests")
    workspace, other, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE workspaces (id uuid PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE users (id uuid PRIMARY KEY)"))
            await conn.execute(
                text("INSERT INTO workspaces (id) VALUES (:a), (:b)"), {"a": workspace, "b": other}
            )
            await conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": user})
            path = Path(__file__).parents[2] / "apps/api/alembic/versions/0012_model_configs.py"
            spec = importlib.util.spec_from_file_location("modelcfg_migration", path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)

            def migrate(sync_conn):
                migration.op = Operations(MigrationContext.configure(sync_conn))
                migration.upgrade()

            await conn.run_sync(migrate)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        principal = Principal(
            user_id=str(user),
            is_authenticated=True,
            workspace_roles={str(workspace): Role.OWNER, str(other): Role.OWNER},
        )

        async def session_dependency():
            async with sessions() as session:
                yield session

        def make_app():
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_session] = session_dependency
            app.dependency_overrides[get_principal] = lambda: principal
            return app

        prefix = f"/v1/workspaces/{workspace}/model-configs"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
        ) as client:
            response = await client.post(
                prefix, json={"name": "saved", "preferred_model": "model-a"}
            )
            assert response.status_code == 201, response.text
            saved = response.json()
            config_id = saved["id"]
            assert (await client.get(prefix)).json() == [saved]
            changed = await client.patch(
                prefix + "/" + config_id, json={"preferred_model": "model-b", "sensitive": True}
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["preferred_model"] == "model-b"
            foreign = await client.get(f"/v1/workspaces/{other}/model-configs/{config_id}")
            assert foreign.status_code == 404
            denied = await client.get(f"/v1/workspaces/{uuid.uuid4()}/model-configs")
            assert denied.status_code == 403
            # Read-only roles may inspect configuration but cannot mutate it.
            principal.workspace_roles[str(workspace)] = Role.VIEWER
            assert (await client.get(prefix)).status_code == 200
            assert (await client.post(prefix, json={"name": "forbidden"})).status_code == 403
            assert (
                await client.patch(prefix + "/" + config_id, json={"sensitive": False})
            ).status_code == 403
            assert (await client.delete(prefix + "/" + config_id)).status_code == 403
            principal.workspace_roles[str(workspace)] = Role.OWNER
            invalid = await client.post(prefix, json={"preferred_model": "x" * 257})
            assert invalid.status_code == 422
        # A fresh app, fresh HTTP client and fresh pooled connections recover the row.
        await engine.dispose()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
        ) as restarted:
            recovered = await restarted.get(prefix + "/" + config_id)
            assert recovered.status_code == 200
            assert recovered.json()["preferred_model"] == "model-b"
            assert recovered.json()["sensitive"] is True
            assert (await restarted.delete(prefix + "/" + config_id)).status_code == 204
            assert (await restarted.get(prefix + "/" + config_id)).status_code == 404
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()
