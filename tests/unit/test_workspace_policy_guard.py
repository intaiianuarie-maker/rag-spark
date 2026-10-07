"""Guards + contract pins for the M1-001c authorization swap (test1, row #15).

The M1-001b interim `workspaces/policy.py` was replaced by the central
`app.authz` service. These guards fail loudly if authorization drifts out of
`app/authz/` again (mirrors the CI authorization guard) and pin the OpenAPI
contract M1-002b consumes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]

APP_DIR = Path("apps/api/app")
WORKSPACES_DIR = APP_DIR / "auth" / "workspaces"
AUTHZ_DIR = APP_DIR / "authz"
ROLE_LITERALS = re.compile(r"[\"'](owner|admin|editor|viewer)[\"']")
ROLE_CONSTANTS = re.compile(r"Role\s*\.\s*(OWNER|ADMIN|EDITOR|VIEWER)")
ROLE_CHECK = re.compile(r"if[^\n]*\.role\s*(==|!=|in\s)")


def _source(repo_root: Path, relative: str | Path) -> str:
    path = repo_root / relative
    assert path.is_file(), f"expected source file missing: {path}"
    return path.read_text(encoding="utf-8")


def test_interim_policy_module_is_gone_and_service_uses_authz(repo_root: Path) -> None:
    assert not (repo_root / WORKSPACES_DIR / "policy.py").exists(), (
        "workspaces/policy.py should have been replaced by app/authz (M1-001c)"
    )
    service = _source(repo_root, WORKSPACES_DIR / "service.py")
    assert "from app.authz import" in service, "service must authorize via app.authz"
    assert re.search(r"\brequire\(", service), "service must call authz.require()"
    assert not ROLE_LITERALS.findall(service), "no hard-coded role literals in service.py"


def test_routers_contain_no_authorization_logic(repo_root: Path) -> None:
    for relative in (
        WORKSPACES_DIR / "router.py",
        APP_DIR / "auth" / "keys_router.py",
    ):
        source = _source(repo_root, relative)
        leaked = {
            "literal role strings": ROLE_LITERALS.findall(source),
            "Role enum constants": ROLE_CONSTANTS.findall(source),
        }
        leaked = {kind: hits for kind, hits in leaked.items() if hits}
        assert not leaked, f"authorization logic leaked into {relative}: {leaked}"


def test_authz_is_the_single_decision_surface(repo_root: Path) -> None:
    source = _source(repo_root, AUTHZ_DIR / "policy.py")
    assert re.search(r"^def can\b", source, re.MULTILINE), "authz.policy must define can()"
    assert re.search(r"^def require\b", source, re.MULTILINE), (
        "authz.policy must define require()"
    )
    assert "class AuthorizationError" in _source(repo_root, AUTHZ_DIR / "errors.py")


def test_no_role_checks_outside_authz(repo_root: Path) -> None:
    """Mirror of the CI authorization guard (M1-001c ticket 'Out')."""
    offenders: list[str] = []
    for path in sorted((repo_root / APP_DIR).rglob("*.py")):
        if "authz" in path.parts:
            continue
        if ROLE_CHECK.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(repo_root)))
    assert not offenders, f"role checks outside app/authz/: {offenders}"


# --------------------------------------------------------------------------------------
# OpenAPI contract pins (M1-002b consumes these)
# --------------------------------------------------------------------------------------


def _openapi(repo_root: Path) -> dict:
    import os

    os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")
    os.environ.setdefault("LOG_JSON", "false")
    os.environ.setdefault("APP_ENV", "test")
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200, response.status_code
    return response.json()


def _enum_for(spec: dict, schema_name: str, property_name: str) -> list[str]:
    schema = spec["components"]["schemas"][schema_name]
    prop = schema["properties"][property_name]
    if "enum" in prop:
        return list(prop["enum"])
    if "$ref" in prop:
        target = prop["$ref"].split("/")[-1]
        return list(spec["components"]["schemas"][target]["enum"])
    for entry in prop.get("allOf", []):
        if "$ref" in entry:
            target = entry["$ref"].split("/")[-1]
            return list(spec["components"]["schemas"][target]["enum"])
    pytest.fail(f"cannot resolve enum for {schema_name}.{property_name}: {prop}")


def test_openapi_workspace_role_enum_is_the_canonical_set(repo_root: Path) -> None:
    spec = _openapi(repo_root)
    roles = _enum_for(spec, "WorkspacePublic", "role")
    assert set(roles) == {"owner", "admin", "editor", "viewer"}, roles
    assert "member" not in roles, "M1-002b must render 'Editor', not 'Member'"


def test_openapi_key_schemas_never_expose_the_secret_on_list(repo_root: Path) -> None:
    spec = _openapi(repo_root)
    schemas = spec["components"]["schemas"]
    assert "ApiKeyPublic" in schemas and "ApiKeyCreated" in schemas
    assert "key" in schemas["ApiKeyCreated"]["properties"]
    assert "key" not in schemas["ApiKeyPublic"]["properties"], (
        "listed keys must never include the full secret"
    )


def test_openapi_endpoint_surface(repo_root: Path) -> None:
    spec = _openapi(repo_root)
    expected = {
        "/v1/workspaces": {"get", "post"},
        "/v1/workspaces/{workspace_id}": {"get", "patch"},
        "/v1/workspaces/{workspace_id}/members": {"post"},
        "/v1/workspaces/{workspace_id}/members/accept": {"post"},
        "/v1/workspaces/{workspace_id}/members/{user_id}": {"patch", "delete"},
        "/v1/workspaces/{workspace_id}/keys": {"get", "post"},
        "/v1/workspaces/{workspace_id}/keys/{key_id}": {"delete"},
    }
    for path, methods in expected.items():
        assert path in spec["paths"], f"missing endpoint {path}"
        missing = methods - set(spec["paths"][path])
        assert not missing, f"{path} missing methods: {missing}"
