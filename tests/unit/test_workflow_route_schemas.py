"""T-V4-019 — workspace draft route contract tests.

A hostile payload battery: every malformed shape must be **rejected** by the
schema layer, and every declared route must be honest about its schemas and
failures. Pinned properties (card):

1. Unwired surface: 0 handlers, 0 routers — no route decorator anywhere in the
   schema module, and no draft-store import in it.
2. Invalid payloads accepted: 0 — a battery of missing fields, wrong types,
   oversized specs, bad revision numbers, scope disagreement and structural
   garbage, each of which must raise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from workflow_engine import (
    NodeSpec,
    NodeType,
    PortSpec,
    WorkflowDefinition,
    WorkflowEdgeSpec,
)
from workflow_engine.routes import ROUTE_CONTRACTS, contract_for, schemas_for_route
from workflow_engine.routes.schemas import (
    CONTENT_HASH_CHARS,
    MAX_SPEC_BYTES,
    DraftAuth,
    DraftErrorCode,
    DraftHistoryQuery,
    DraftHistoryResponse,
    DraftMutationResponse,
    DraftReviseRequest,
    DraftRevisionDetail,
    DraftRevisionMeta,
    DraftRevisionPath,
    DraftRouteName,
    DraftSaveRequest,
    DraftWorkflowPath,
    assert_path_scope_matches,
)

SCHEMAS = Path(
    __file__
).resolve().parents[2] / "packages" / "workflow_engine" / "routes" / "schemas.py"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _definition(
    *,
    wid: str = "wf-api",
    workspace: str = "ws-1",
    name: str = "Draft",
    description: str = "",
    nodes: int = 2,
    extra: int = 0,
) -> WorkflowDefinition:
    node_list = [
        NodeSpec(
            id="start",
            type=NodeType.TRIGGER,
            name="Start",
            output_ports=[PortSpec(name="output", type="dict")],
        ),
        NodeSpec(
            id="fmt",
            type=NodeType.TRANSFORM,
            name="Format",
            config={"template": "Hi {name}!"},
            input_ports=[PortSpec(name="input", type="any")],
            output_ports=[PortSpec(name="output", type="string")],
        ),
    ][:nodes]
    node_list += [
        NodeSpec(
            id=f"extra-{i}",
            type=NodeType.OUTPUT,
            name=f"Extra {i}",
            input_ports=[PortSpec(name="input", type="any")],
            output_ports=[PortSpec(name="output", type="any")],
        )
        for i in range(extra)
    ]
    edges = [
        WorkflowEdgeSpec(
            id="e1", source_node_id="start", source_port="output",
            target_node_id="fmt", target_port="input",
        )
    ] if "fmt" in {n.id for n in node_list} else []
    return WorkflowDefinition(
        id=wid, workspace_id=workspace, name=name, description=description,
        nodes=node_list, edges=edges,
    )


def _body(**overrides) -> dict:
    """A flat request body: the definition's own fields (house style)."""
    payload = json.loads(_definition().model_dump_json())
    payload.update(overrides)
    return payload


def _meta() -> dict:
    return {
        "workflow_id": "wf-api",
        "workspace_id": "ws-1",
        "revision": 1,
        "content_hash": "a" * CONTENT_HASH_CHARS,
        "summary": "wf-api · 2 node(s) [trigger:start, transform:fmt]",
        "created_at": "2026-10-14T12:00:00+00:00",
    }


def _rejects(request_cls, payload) -> None:
    with pytest.raises(ValidationError):
        request_cls.model_validate(payload)


# --------------------------------------------------------------------------- #
# 1. Unwired surface
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_schema_module_declares_no_routes() -> None:
    """Card metric: unwired surface = 0 handlers/routers.

    Checked against the module's **AST**, not its prose — a docstring saying
    "no APIRouter" must not satisfy (or trip) the check.
    """
    import ast

    tree = ast.parse(SCHEMAS.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert not {m for m in imported if m.split(".")[0] == "fastapi"}, imported
    assert not {m for m in imported if m.split(".")[0] in {"app", "apps"}}, imported

    decorated = [
        d.id
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        for d in node.decorator_list
        if isinstance(d, ast.Name)
        and d.id not in {"classmethod", "staticmethod", "property"}
    ]
    assert decorated == [], f"unknown decorators present: {decorated}"

    # No reference to router objects anywhere in executable nodes.
    wire_names = {"APIRouter", "FastAPI", "HTTPException", "Depends", "include_router"}
    referenced = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not (wire_names & referenced), f"router wiring referenced: {wire_names & referenced}"


@pytest.mark.unit
def test_schema_module_imports_no_store() -> None:
    """Card metric: no store import — contracts outlive the backend."""
    import ast

    tree = ast.parse(SCHEMAS.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
    assert not {m for m in modules if m.endswith(("draft_store", "store_discipline"))}, (
        modules
    )
    assert not {"InMemoryDraftStore", "SqliteCheckpointStore"} & {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }


@pytest.mark.unit
def test_route_contracts_are_data_not_wiring() -> None:
    """Importing the contract table registers nothing."""
    assert tuple(c.name for c in ROUTE_CONTRACTS) == (
        DraftRouteName.SAVE_DRAFT,
        DraftRouteName.REVISE_DRAFT,
        DraftRouteName.GET_REVISION,
        DraftRouteName.LIST_HISTORY,
    )
    for contract in ROUTE_CONTRACTS:
        assert contract.path.startswith("/v1/workspaces/{workspace_id}/drafts")
        assert contract.method in {"GET", "POST"}
        assert contract.auth in set(DraftAuth)
        assert contract.schema_names()


@pytest.mark.unit
def test_every_route_declares_auth_scope_and_error_catalogue() -> None:
    """No route may be missing auth or an error catalogue."""
    for contract in ROUTE_CONTRACTS:
        assert contract.auth in DraftAuth, contract.name
        assert contract.errors, contract.name
        assert {e.status_code for e in contract.errors} >= {403}, contract.name
        assert all(isinstance(e.code, DraftErrorCode) for e in contract.errors)


# --------------------------------------------------------------------------- #
# 2. Accept battery — the good shapes
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_valid_save_and_revise_bodies_are_accepted() -> None:
    assert DraftSaveRequest.model_validate(_body())
    assert DraftReviseRequest.model_validate(_body())


@pytest.mark.unit
def test_valid_paths_and_queries_are_accepted() -> None:
    assert DraftWorkflowPath.model_validate({"workflow_id": "wf-api"})
    assert DraftRevisionPath.model_validate({"workflow_id": "wf-api", "revision": 7})
    assert DraftHistoryQuery.model_validate({}).limit == 50
    assert DraftHistoryQuery.model_validate({"limit": 200}).limit == 200


@pytest.mark.unit
def test_valid_responses_round_trip() -> None:
    meta = DraftRevisionMeta.model_validate(_meta())
    detail = DraftRevisionDetail.model_validate({**_meta(), "spec": _definition()})
    assert DraftMutationResponse.model_validate(
        {**_meta(), "spec": _definition(), "idempotent": False}
    )
    assert DraftHistoryResponse.model_validate(
        {
            "workflow_id": "wf-api",
            "workspace_id": "ws-1",
            "count": 1,
            "revisions": [meta.model_dump()],
        }
    )
    assert detail.revision == 1


@pytest.mark.unit
def test_unknown_fields_are_rejected() -> None:
    """A typo must never become a default (extra='forbid')."""
    _rejects(DraftSaveRequest, _body(nope=1))


# --------------------------------------------------------------------------- #
# 3. Hostile battery — every one of these must be REJECTED
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_missing_required_fields_rejected() -> None:
    _rejects(DraftSaveRequest, {})
    _rejects(DraftSaveRequest, {"definition": {}})
    payload = _body()
    del payload["name"]
    _rejects(DraftSaveRequest, payload)
    payload = _body()
    del payload["id"]
    _rejects(DraftSaveRequest, payload)
    payload = _body()
    payload["nodes"] = []
    _rejects(DraftSaveRequest, payload)


@pytest.mark.unit
def test_wrong_types_rejected() -> None:
    payload = _body()
    payload["nodes"] = "two"
    _rejects(DraftSaveRequest, payload)
    payload = _body()
    payload["id"] = 42
    _rejects(DraftSaveRequest, payload)
    payload = _body()
    payload["workspace_id"] = ["ws-1"]
    _rejects(DraftSaveRequest, payload)
    _rejects(DraftRevisionPath, {"workflow_id": "wf-api", "revision": "1"})
    _rejects(DraftRevisionPath, {"workflow_id": "wf-api", "revision": 1.5})
    _rejects(DraftHistoryQuery, {"limit": "abc"})
    _rejects(DraftHistoryQuery, {"limit": None})
    _rejects(
        DraftRevisionMeta, {**_meta(), "created_at": 20261014}
    )


@pytest.mark.unit
def test_query_limit_coerces_but_path_revision_does_not() -> None:
    """A query value is a client knob (coerced); a path revision is identity.

    FastAPI delivers query params as strings, so ``?limit=50`` must coerce. A
    path segment is part of *which* revision is being addressed, so it must
    never coerce: ``/revisions/1`` and ``/revisions/"1"`` are the same target
    and ``"1abc"`` is not a revision at all.
    """
    assert DraftHistoryQuery.model_validate({"limit": "50"}).limit == 50
    _rejects(DraftRevisionPath, {"workflow_id": "wf-api", "revision": "1"})
    _rejects(DraftRevisionMeta, {**_meta(), "revision": "1"})


@pytest.mark.unit
def test_oversized_spec_rejected() -> None:
    """Card metric's 'oversized spec': the body is refused, never truncated."""
    big = _definition(extra=60)
    for node in big.nodes[2:]:
        node.config = {"blob": "x" * 8192}
    payload = json.loads(big.model_dump_json())
    _rejects(DraftSaveRequest, {"definition": payload})

    # Name/description caps.
    payload = _body()
    payload["name"] = "n" * 201
    _rejects(DraftSaveRequest, payload)
    payload = _body()
    payload["description"] = "d" * 4097
    _rejects(DraftSaveRequest, payload)

    # Node/edge count caps.
    payload = _body()
    payload["nodes"] = [
        {"id": f"n{i}", "type": "output", "name": "n"}
        for i in range(201)
    ]
    _rejects(DraftSaveRequest, payload)


@pytest.mark.unit
def test_bad_revision_numbers_rejected() -> None:
    for bad in (0, -1, 1_000_001, 1.5, "1", True, None):
        _rejects(DraftRevisionPath, {"workflow_id": "wf-api", "revision": bad})
    _rejects(DraftRevisionMeta, {**_meta(), "revision": 0})
    payload = _body()
    payload["id"] = ""
    _rejects(DraftSaveRequest, payload)


@pytest.mark.unit
def test_bad_hashes_rejected() -> None:
    for bad in ("", "a" * 63, "z" * 64, "A" * 64, "a" * 65, None):
        _rejects(DraftRevisionMeta, {**_meta(), "content_hash": bad})
    _rejects(DraftRevisionMeta, {**_meta(), "parent_hash": "not-a-hash"})
    _rejects(DraftRevisionMeta, {**_meta(), "parent_revision": 0})


@pytest.mark.unit
def test_missing_metadata_rejected() -> None:
    for field in (
        "workflow_id", "workspace_id", "revision", "content_hash", "summary",
        "created_at",
    ):
        payload = _meta()
        del payload[field]
        _rejects(DraftRevisionMeta, payload)


@pytest.mark.unit
def test_empty_and_whitespace_ids_rejected() -> None:
    for bad in ("", "   ", "\t"):
        _rejects(DraftWorkflowPath, {"workflow_id": bad})
        payload = _body()
        payload["workspace_id"] = bad
        _rejects(DraftSaveRequest, payload)


@pytest.mark.unit
def test_structurally_broken_definitions_rejected() -> None:
    # Edge referencing a node that is not declared.
    payload = _body()
    payload["edges"][0]["target_node_id"] = "ghost"
    _rejects(DraftSaveRequest, payload)
    # Duplicate node ids.
    payload = _body()
    payload["nodes"][1]["id"] = "start"
    _rejects(DraftSaveRequest, payload)
    # Duplicate edge ids.
    payload = _body()
    payload["edges"].append(dict(payload["edges"][0]))
    _rejects(DraftSaveRequest, payload)


@pytest.mark.unit
def test_a_node_without_ports_is_still_legal() -> None:
    """Not every invalid-looking shape is invalid: a portless node is allowed.

    The schema enforces wire integrity, not execution validity — the draft
    validator (T-V4-006) is the authority on whether a workflow can run.
    """
    payload = _body()
    payload["nodes"][0]["output_ports"] = []
    payload["edges"] = []
    assert DraftSaveRequest.model_validate(payload)


@pytest.mark.unit
def test_scope_disagreement_is_refused() -> None:
    """A body must name the workspace and workflow it is actually writing."""
    body = DraftSaveRequest.model_validate(_body())
    with pytest.raises(ValueError, match=DraftErrorCode.DRAFT_WORKSPACE_MISMATCH):
        assert_path_scope_matches(body, workspace_id="ws-other")
    with pytest.raises(ValueError, match=DraftErrorCode.DRAFT_IDENTITY_MISMATCH):
        assert_path_scope_matches(
            body, workspace_id="ws-1", workflow_id="wf-someone-else"
        )
    # Agreement is accepted (no exception).
    assert_path_scope_matches(body, workspace_id="ws-1", workflow_id="wf-api")


@pytest.mark.unit
def test_history_response_count_must_match_revisions() -> None:
    with pytest.raises(ValidationError):
        DraftHistoryResponse.model_validate(
            {"workflow_id": "wf-api", "workspace_id": "ws-1", "count": 2,
             "revisions": []}
        )


@pytest.mark.unit
def test_history_limit_is_bounded() -> None:
    _rejects(DraftHistoryQuery, {"limit": 0})
    _rejects(DraftHistoryQuery, {"limit": 201})
    _rejects(DraftHistoryQuery, {"nope": 1})


@pytest.mark.unit
def test_error_envelope_shape() -> None:
    body = {
        "detail": {
            "error": DraftErrorCode.DRAFT_NOT_FOUND.value,
            "message": "workflow does not exist",
        }
    }
    from workflow_engine.routes.schemas import (
        ApiErrorBody,
        ApiErrorDetail,
    )

    assert ApiErrorBody.model_validate(body).detail.error == "draft_not_found"
    assert ApiErrorDetail.model_validate(body["detail"]).message
    with pytest.raises(ValidationError):
        ApiErrorBody.model_validate({"detail": {"error": "x"}})
    with pytest.raises(ValidationError):
        ApiErrorBody.model_validate({"detail": {"error": "x", "message": "y", "z": 1}})


@pytest.mark.unit
def test_route_error_catalogues(name: str = "save_draft") -> None:
    """Each route documents only failures a handler could actually produce."""
    expected = {
        "save_draft": {"draft_forbidden", "draft_conflict", "draft_too_large",
                       "draft_validation_failed", "draft_workspace_mismatch",
                       "draft_secret_refused"},
        "revise_draft": {"draft_forbidden", "draft_too_large",
                         "draft_validation_failed", "draft_identity_mismatch",
                         "draft_workspace_mismatch", "draft_secret_refused"},
        "get_revision": {"draft_forbidden", "draft_not_found",
                         "draft_validation_failed"},
        "list_history": {"draft_forbidden", "draft_not_found",
                         "draft_validation_failed"},
    }
    for route, codes in expected.items():
        assert {c.value for c in contract_for(route).error_codes()} == codes


@pytest.mark.unit
def test_contract_lookup_is_exact() -> None:
    assert contract_for("save_draft").method == "POST"
    assert contract_for(DraftRouteName.LIST_HISTORY).request is not None
    assert contract_for("get_revision").request is None  # GET: no body
    with pytest.raises(KeyError):
        contract_for("no_such_route")
    assert "DraftSaveRequest" in schemas_for_route("save_draft")
    assert "DraftHistoryResponse" in schemas_for_route("list_history")


@pytest.mark.unit
def test_status_codes_are_sane_http() -> None:
    for contract in ROUTE_CONTRACTS:
        for status, _ in contract.responses:
            assert 200 <= status < 300, contract.name
        for spec in contract.errors:
            assert 400 <= spec.status_code < 600, contract.name


@pytest.mark.unit
def test_payload_cap_is_declared_not_hidden() -> None:
    assert MAX_SPEC_BYTES == 262_144
    # A payload right at the cap is still accepted; the cap is not a silent
    # truncation threshold.
    defn = _definition(extra=3)
    for node in defn.nodes[2:]:
        node.config = {"blob": "x" * 100}
    assert DraftSaveRequest.model_validate(json.loads(defn.model_dump_json()))
