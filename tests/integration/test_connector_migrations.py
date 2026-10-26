"""Exercise the actual connector migration chain in a private PostgreSQL schema."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from core.config import get_settings
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration


def test_connector_migration_upgrade_downgrade_upgrade():
    schema = "connector_migration_" + uuid.uuid4().hex
    url = get_settings().database_url.replace("+asyncpg", "+psycopg")
    admin = create_engine(url, connect_args={"connect_timeout": 3}, poolclass=NullPool)
    try:
        with admin.connect():
            pass
    except OperationalError:
        admin.dispose()
        pytest.skip("PostgreSQL unavailable; connector migration requires isolated schema")
    scoped = create_engine(
        url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    api = Path(__file__).parents[2] / "apps" / "api"
    # Only the subprocess receives these values. Parent settings/pools and the
    # shared public schema cannot be redirected by the test.
    environment = {
        **os.environ,
        "DATABASE_URL": url,
        "PGOPTIONS": f"-csearch_path={schema}",
        "PYTHONPATH": os.pathsep.join([str(api.parents[1] / "packages"), str(api)]),
    }

    def migrate(action, revision):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", action, revision],
            cwd=api,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        migrate("upgrade", "head")
        with scoped.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0017_sync_failure_markers"
            )
            database = inspect(conn)
            mapping = {column["name"] for column in database.get_columns("sync_source_documents")}
            assert {"external_id", "external_id_key", "document_id", "workspace_id"} <= mapping
            primary_key = database.get_pk_constraint("sync_source_documents")["constrained_columns"]
            assert primary_key == ["workspace_id", "connector_id", "external_id_key"]
            progress = {
                column["name"] for column in database.get_columns("connector_sync_progress")
            }
            assert {"last_error_code", "last_finished_at", "items_deleted"} <= progress
        migrate("downgrade", "0015_connector_configs")
        with scoped.connect() as conn:
            database = inspect(conn)
            assert "connector_configs" in database.get_table_names()
            assert "sync_source_documents" not in database.get_table_names()
            progress = {
                column["name"] for column in database.get_columns("connector_sync_progress")
            }
            assert "last_error_code" not in progress and "last_finished_at" not in progress
        migrate("upgrade", "head")
        with scoped.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0017_sync_failure_markers"
            )
    finally:
        scoped.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
