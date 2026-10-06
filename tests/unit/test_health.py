"""Unit tests: config, interface imports, and system endpoints.

These must pass with no external services running.
"""

from __future__ import annotations

import importlib
import os

import pytest

# Keep the app from touching the DB during unit tests, and set before importing
# the application so settings are cached with these values.
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("APP_ENV", "test")

pytestmark = pytest.mark.unit



def test_config_loads_with_defaults() -> None:
    from core.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.app_name == "knowledge-os-api"
    assert settings.redis_url.startswith("redis://")
    assert settings.qdrant_url.startswith("http")
    assert settings.minio_bucket
    assert settings.cors_origin_list == ["http://localhost:3000"]


def test_config_parses_comma_separated_cors() -> None:
    from core.config import Settings

    settings = Settings(
        cors_origins="http://a.test, http://b.test", _env_file=None  # type: ignore[call-arg]
    )
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


def test_interface_modules_import_without_services() -> None:
    """Contracts must import with stdlib/pydantic only (no vendor SDKs)."""
    modules = [
        "ingestion.interfaces",
        "retrieval.interfaces",
        "models.interfaces",
        "connectors.interfaces",
        "core.auth",
        "core.health",
        "core.config",
        "core.logging",
    ]
    for module in modules:
        importlib.import_module(module)


def test_ingestion_document_element_shape() -> None:
    from ingestion.interfaces import DocumentElement, ElementType

    element = DocumentElement(type=ElementType.TEXT, text="hello", page=1, section="Intro")
    assert element.page == 1
    assert element.section == "Intro"
    assert element.bbox is None
    assert element.metadata == {}


def test_acl_scope_allows_only_assigned_workspaces() -> None:
    from core.auth import ACLScope

    scope = ACLScope(user_id="u1", workspace_ids=["ws-a"])
    assert scope.allows_workspace("ws-a")
    assert not scope.allows_workspace("ws-b")
    assert not scope.is_empty


def test_health_endpoint_ok() -> None:
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "knowledge-os-api"
    assert "version" in body


def test_version_endpoint_ok() -> None:
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        response = client.get("/version")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "knowledge-os-api"
    assert "git_sha" in body


def test_request_id_header_echoed() -> None:
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        response = client.get("/health", headers={"x-request-id": "abc123"})
    assert response.headers.get("x-request-id") == "abc123"
