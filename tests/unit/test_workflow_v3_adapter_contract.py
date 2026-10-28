"""T-V4-014 — V3 execution-adapter seam contract tests.

Pinned properties (card):
1. **Contract violations constructible = 0.** A hostile battery over requests,
   responses, capability manifests, errors and limits: malformed shapes, unknown
   statuses/codes, missing capability entries, contradictory flags — each must
   be *rejected* at the contract boundary, never coerced into a success.
2. **V3 imports added = 0.** The contract declares V3's vocabulary by value and
   never imports ``packages.agents``; the existing adapter keeps its Protocol
   untouched.
3. **Existing adapter behaviour change = 0.** The T-V4-015 (adapter) and
   T-V4-016 (checkpoint) suites still pass unchanged, and the adapter's request
   shape is byte-identical to what it built before this module existed.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from workflow_engine import (
    CapabilityManifest,
    NodeSpec,
    NodeType,
    PortSpec,
)
from workflow_engine.adapters.v3_contract import (
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    V3ContractError,
    V3DispatchLimits,
    V3ErrorCode,
    V3ErrorDetail,
    V3NodeRequest,
    V3NodeResponse,
    V3RunStatus,
    assert_capability_allowed,
    assert_no_v3_import,
    map_error,
    map_manifest,
    map_response,
    propagate_cancellation,
    validate_dispatch,
)

CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "packages" / "workflow_engine" / "adapters" / "v3_contract.py"
)
ADAPTER = (
    Path(__file__).resolve().parents[2]
    / "packages" / "workflow_engine" / "adapters" / "v3_adapter.py"
)
PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "packages" / "workflow_engine"


def _node(
    *,
    node_id: str = "agent-1",
    node_type: NodeType = NodeType.AGENT,
    config: dict | None = None,
    capabilities: list[str] | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        name="Agent",
        config=config or {"prompt": "Summarise {input}"},
        input_ports=[PortSpec(name="input", type="any")],
        output_ports=[PortSpec(name="output", type="any")],
        required_capabilities=list(capabilities or ["agent.run"]),
    )


def _request(**overrides) -> dict:
    payload = {
        "node_id": "agent-1",
        "node_type": "agent",
        "node_config": {"prompt": "Summarise {input}"},
        "workspace_id": "ws-1",
        "input_data": {"input": "text"},
        "required_capabilities": ["agent.run"],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# 1. Request contract
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_request_matches_what_the_existing_adapter_builds() -> None:
    """The contract must describe the seam that already works, not a new one."""
    node = _node()
    request = V3NodeRequest.from_node(node, {"input": "text"}, "ws-1")

    expected_keys = {
        "node_id", "node_type", "node_config", "workspace_id", "input_data",
        "required_capabilities",
    }
    assert set(request.model_dump()) == expected_keys
    assert request.node_id == node.id
    assert request.node_type == str(node.type)
    assert request.node_config == dict(node.config)
    assert request.workspace_id == "ws-1"
    assert request.required_capabilities == list(node.required_capabilities)


@pytest.mark.unit
def test_malformed_requests_are_rejected() -> None:
    hostile = {
        "missing node_id": {k: v for k, v in _request().items() if k != "node_id"},
        "missing workspace_id": {
            k: v for k, v in _request().items() if k != "workspace_id"
        },
        "blank node_id": _request(node_id="   "),
        "blank workspace_id": _request(workspace_id=""),
        "unknown node type": _request(node_type="quantum"),
        "non-integer capabilities": _request(required_capabilities=[42]),
        "duplicate capabilities": _request(required_capabilities=["a", "a"]),
        "too many capabilities": _request(required_capabilities=[f"c{i}" for i in range(40)]),
        "input not a mapping": _request(input_data=["a", "b"]),
        "config not a mapping": _request(node_config="oops"),
        "renamed key": {**_request(), "nodeId": "agent-1"},
        "extra key": {**_request(), "trace": "abc"},
        "node_id not a string": _request(node_id=7),
    }
    for label, payload in hostile.items():
        with pytest.raises(ValidationError) as excinfo:
            V3NodeRequest.model_validate(payload)
        assert excinfo.value.errors(), label


@pytest.mark.unit
@pytest.mark.parametrize(
    "node_type",
    [t for t in NodeType],
)
def test_every_workflow_node_type_is_accepted(node_type: NodeType) -> None:
    """The contract must not exclude any node type the executor can run."""
    node = _node(node_type=node_type)
    request = V3NodeRequest.from_node(node, {}, "ws-1")
    assert request.node_type == node_type.value


@pytest.mark.unit
def test_request_payload_is_bounded() -> None:
    """An oversized request is refused, never truncated (T-V4-020 discipline)."""
    from workflow_engine.adapters.v3_contract import MAX_REQUEST_BYTES

    ok = V3NodeRequest.model_validate(_request(input_data={"blob": "x" * 1024}))
    assert ok.json_size() < MAX_REQUEST_BYTES

    with pytest.raises(ValidationError, match="more than"):
        V3NodeRequest.model_validate(
            _request(input_data={"blob": "x" * (MAX_REQUEST_BYTES + 4096)})
        )


# --------------------------------------------------------------------------- #
# 2. Response contract
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_well_formed_responses_are_accepted() -> None:
    ok = V3NodeResponse.model_validate(
        {"status": "completed", "output_data": {"output": "done"}}
    )
    assert ok.workflow_outcome() == "succeeded"
    # Cancelled: a flag plus the cancelled error code — there is no cancelled
    # run status in V3.
    cancelled = V3NodeResponse.model_validate(
        {
            "status": "failed",
            "cancelled": True,
            "error": {"code": "cancelled", "message": "stopped by caller"},
        }
    )
    assert cancelled.workflow_outcome() == "cancelled"
    assert map_response(cancelled, node_id="n")[0] == "cancelled"


@pytest.mark.unit
def test_malformed_responses_are_rejected() -> None:
    hostile = {
        "no status": {"output_data": {}},
        "unknown status": {"status": "almost"},
        "non-terminal status": {"status": "pending"},
        "blank status": {"status": ""},
        "status not a string": {"status": 3},
        "unknown error code": {
            "status": "failed",
            "error": {"code": "mystery_failure", "message": "?"},
        },
        "completed with an error": {
            "status": "completed",
            "error": {"code": "provider_error"},
        },
        "cancelled status without the flag": {"status": "cancelled"},
        "flag without a cancelled status": {"status": "failed", "cancelled": True},
        "extra key": {"status": "completed", "trace": "x"},
        "error not an object": {"status": "failed", "error": "provider_error"},
        "output not a mapping": {"status": "completed", "output_data": ["x"]},
    }
    for label, payload in hostile.items():
        with pytest.raises(ValidationError) as excinfo:
            V3NodeResponse.model_validate(payload)
        assert excinfo.value.errors(), label


@pytest.mark.unit
def test_a_cancelled_response_is_never_a_failure() -> None:
    """Cancellation is its own outcome: never recorded as FAILED."""
    response = V3NodeResponse.model_validate(
        {"status": "failed", "cancelled": True, "error": {"code": "cancelled"}}
    )
    assert response.workflow_outcome() == "cancelled"
    # A failure dressed as a cancellation is refused: the flag must carry the
    # cancelled error code, never a provider error.
    with pytest.raises(ValidationError, match="CANCELLED"):
        V3NodeResponse.model_validate(
            {"status": "failed", "cancelled": True,
             "error": {"code": "provider_error"}}
        )
    # A cancelled flag with no error at all is refused too.
    with pytest.raises(ValidationError, match="must carry the cancelled error"):
        V3NodeResponse.model_validate({"status": "failed", "cancelled": True})


@pytest.mark.unit
def test_every_declared_status_maps_to_an_outcome() -> None:
    """No terminal V3 status is unmapped in the contract's table."""
    for status in V3RunStatus:
        needs_error = status in {V3RunStatus.FAILED, V3RunStatus.BUDGET_EXCEEDED,
                                 V3RunStatus.MAX_STEPS_EXCEEDED}
        response = V3NodeResponse.model_validate(
            {
                "status": status.value,
                **({"error": {"code": "provider_error"}} if needs_error else {}),
            }
        )
        outcome, _mapped = map_response(response, node_id="agent-1")
        assert outcome in {"succeeded", "failed"}, status
        if status == V3RunStatus.COMPLETED:
            assert outcome == "succeeded"
        else:
            assert outcome == "failed"


# --------------------------------------------------------------------------- #
# 3. Error taxonomy mapping — nothing unmapped, nothing swallowed
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("code", [c for c in V3ErrorCode])
def test_every_error_code_maps(code: V3ErrorCode) -> None:
    """Every declared V3 error code has a workflow outcome and names the node."""
    mapped = map_error(V3ErrorDetail(code=code), node_id="agent-1")
    assert mapped.node_id == "agent-1"
    assert mapped.outcome in {"succeeded", "failed", "cancelled"}
    if code == V3ErrorCode.CANCELLED:
        assert mapped.outcome == "cancelled"


@pytest.mark.unit
def test_no_error_code_maps_to_success() -> None:
    """The swallow check: an error must never look like a node that succeeded."""
    for code in V3ErrorCode:
        mapped = map_error(V3ErrorDetail(code=code), node_id="n")
        assert mapped.outcome != "succeeded", code


@pytest.mark.unit
def test_an_error_must_name_its_node() -> None:
    """A blank node id is a contract violation, not an anonymous failure."""
    for bad_node_id in ("", "   "):
        with pytest.raises(V3ContractError, match="must name the node"):
            map_error(
                V3ErrorDetail(code=V3ErrorCode.PROVIDER_ERROR),
                node_id=bad_node_id,
            )


@pytest.mark.unit
def test_declared_vocabulary_still_matches_live_v3() -> None:
    """The contract mirrors V3 by value; a V3 rename must fail loudly here.

    The V3 module is imported *in this test only* (never by the contract), so
    drift between the contract and V3 is a test failure rather than a silent
    seam divergence.
    """
    packages = Path(__file__).resolve().parents[2] / "packages"
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(packages)!r});"
        "from agents.interfaces import AgentErrorCode, AgentRunStatus;"
        "import json;"
        "print(json.dumps(sorted(c.value for c in AgentErrorCode)));"
        "print(json.dumps(sorted(s.value for s in AgentRunStatus)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    lines = [line for line in out.stdout.splitlines() if line.startswith("[")]
    assert len(lines) == 2, f"unexpected V3 vocabulary output: {out.stdout!r}"
    live_errors, live_statuses = (json.loads(line) for line in lines)

    assert sorted(c.value for c in V3ErrorCode) == live_errors
    declared = {s.value for s in V3RunStatus}
    non_terminal = {"pending", "running"}
    # The contract declares exactly V3's terminal statuses — no invented member
    # (a bridge could never produce it) and no non-terminal one (it is not a
    # result).
    assert declared == set(live_statuses) - non_terminal
    assert not (declared & non_terminal), declared & non_terminal
    assert "completed" in declared
    # Cancellation lives in the error taxonomy, not the status vocabulary.
    assert "cancelled" not in declared
    assert "cancelled" in live_errors


# --------------------------------------------------------------------------- #
# 4. Capability manifest mapping
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_capability_entry_is_required_for_every_required_capability() -> None:
    manifest = CapabilityManifest(
        supported_node_types={NodeType.AGENT},
        enabled_capabilities={"agent.run"},
    )
    node = _node(capabilities=["agent.run"])
    mapping = map_manifest(manifest, node)
    assert mapping.allowed
    assert mapping.missing == ()
    assert_capability_allowed(manifest, node)


@pytest.mark.unit
def test_missing_capability_entry_is_refused() -> None:
    manifest = CapabilityManifest(enabled_capabilities={"agent.run"})
    node = _node(capabilities=["agent.run", "web.browse"])
    mapping = map_manifest(manifest, node)
    assert not mapping.allowed
    assert mapping.missing == ("web.browse",)
    with pytest.raises(V3ContractError, match="unavailable"):
        assert_capability_allowed(manifest, node)


@pytest.mark.unit
def test_an_empty_manifest_is_refused_not_vacuously_allowed() -> None:
    """An empty manifest would pass every capability check vacuously."""
    empty = CapabilityManifest()
    node = _node(capabilities=[])
    with pytest.raises(V3ContractError, match="vacuously"):
        map_manifest(empty, node)


@pytest.mark.unit
def test_unused_capabilities_are_reported_not_hidden() -> None:
    """A mapping that grants more than asked is visible, not silently ignored."""
    manifest = CapabilityManifest(enabled_capabilities={"agent.run", "extra"})
    node = _node(capabilities=["agent.run"])
    mapping = map_manifest(manifest, node)
    assert mapping.allowed
    assert mapping.unused == ("extra",)


# --------------------------------------------------------------------------- #
# 5. Timeout / cancel propagation
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_dispatch_limits_are_validated_not_clamped() -> None:
    assert validate_dispatch(V3DispatchLimits(timeout_seconds=5)).timeout_seconds == 5
    for bad in (0, -1, MIN_TIMEOUT_SECONDS / 2, MAX_TIMEOUT_SECONDS * 2, float("nan")):
        with pytest.raises(V3ContractError):
            validate_dispatch(V3DispatchLimits(timeout_seconds=bad))


@pytest.mark.unit
def test_a_finished_run_propagates_nothing() -> None:
    assert propagate_cancellation(node_id="n", cancelled=False, timed_out=False) is None


@pytest.mark.unit
def test_cancel_and_deadline_propagate_to_a_cancelled_outcome() -> None:
    cancel = propagate_cancellation(node_id="agent-1", cancelled=True, timed_out=False)
    assert cancel is not None
    assert cancel.outcome == "cancelled"
    assert cancel.node_id == "agent-1"
    assert cancel.code == V3ErrorCode.CANCELLED

    deadline = propagate_cancellation(node_id="agent-2", cancelled=False, timed_out=True)
    assert deadline is not None
    assert deadline.outcome == "cancelled"
    assert deadline.code == V3ErrorCode.DEADLINE_EXCEEDED
    # A deadline is a cancellation, not a failure.
    assert deadline.outcome != "failed"


# --------------------------------------------------------------------------- #
# 6. No V3 import, no behaviour change
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_contract_does_not_import_v3() -> None:
    """Card metric: V3 imports added = 0 (source-level)."""
    import ast

    source = CONTRACT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")

    v3 = {m for m in modules if m.split(".")[0] in {"agents", "packages"}}
    assert not v3, f"contract imports V3: {v3}"


@pytest.mark.unit
def test_the_contract_has_no_runtime_v3_dependency() -> None:
    """Card metric: even loaded, nothing in it resolves into V3 (assert helper)."""
    import workflow_engine.adapters.v3_contract as contract

    # The contract's own globals pass, and a deliberately poisoned global fails.
    assert_no_v3_import(contract.__dict__)
    with pytest.raises(V3ContractError):
        assert_no_v3_import({"__name__": "agents.runtime"})

    # A module inside V3 cannot host the contract either.
    class _Fake:
        __module__ = "agents.interfaces"

    with pytest.raises(V3ContractError):
        assert_no_v3_import({"leaked": _Fake()})


@pytest.mark.unit
def test_the_existing_adapter_is_untouched_by_this_contract() -> None:
    """Card metric: existing adapter behaviour change = 0."""
    from workflow_engine.adapters import v3_adapter

    # The Protocol the adapter relies on still exists with its original shape.
    assert hasattr(v3_adapter, "AgentRunner")
    assert callable(v3_adapter.AgentRunner)
    signature = inspect.signature(v3_adapter.AgentRunner.__call__)
    # (self, request) — exactly what the Protocol declared in T-V4-015.
    assert list(signature.parameters) == ["self", "request"]
    assert signature.parameters["request"].annotation

    # The adapter still refuses while flagged off (unchanged semantics).
    assert v3_adapter.adapter_enabled({"WORKFLOW_V3_ADAPTER": "0"}) is False
    assert v3_adapter.adapter_enabled({}) is False
    assert v3_adapter.adapter_enabled({"WORKFLOW_V3_ADAPTER": "1"}) is True


@pytest.mark.unit
async def test_adapter_still_dispatches_the_same_request_shape() -> None:
    """The request the contract types is the request the adapter already sends."""
    from workflow_engine.adapters.v3_adapter import (
        V3NodeExecutor,
    )

    seen: list[dict] = []

    async def spy(request):
        seen.append(json.loads(json.dumps(request)))
        return {"status": "completed", "output_data": {"output": "ok"}}

    node = _node()
    executor = V3NodeExecutor(
        agent_runner=spy,
        manifest=CapabilityManifest(enabled_capabilities={"agent.run"}),
        enabled=True,
    )
    await executor.execute_node(node, {"input": "text"}, "ws-1")

    assert executor.dispatches == 1
    assert set(seen[0]) == set(V3NodeRequest.model_validate(seen[0]).model_dump())
    # The contract must accept, verbatim, what the adapter dispatched.
    V3NodeRequest.model_validate(seen[0])


@pytest.mark.unit
async def test_the_adapter_refuses_while_flagged_off() -> None:
    """Unchanged: a disabled adapter raises, and dispatches stay 0."""
    from workflow_engine.adapters.v3_adapter import V3AdapterDisabled, V3NodeExecutor

    async def spy(request):
        raise AssertionError("must not dispatch while disabled")

    executor = V3NodeExecutor(agent_runner=spy, enabled=False)
    with pytest.raises(V3AdapterDisabled):
        await executor.execute_node(_node(), {}, "ws-1")
    assert executor.dispatches == 0


@pytest.mark.unit
def test_contract_is_only_types_and_pure_functions() -> None:
    """No bridge callable is built: the module exposes no dispatch entry point."""
    import workflow_engine.adapters.v3_contract as contract

    forbidden = ("dispatch", "bridge", "run_node", "execute", "call_v3", "invoke")
    for name in dir(contract):
        if name.startswith("_"):
            continue
        assert name not in forbidden, f"{name} looks like a bridge entry point"


@pytest.mark.asyncio
async def test_a13_malformed_response_refused() -> None:
    from pydantic import ValidationError
    from workflow_engine.adapters.v3_adapter import V3NodeExecutor

    async def spy(request):
        return {"foo": "bar"}

    exec_ = V3NodeExecutor(
        agent_runner=spy,
        manifest=CapabilityManifest(enabled_capabilities={"agent.run"}),
        enabled=True,
    )
    with __import__("pytest").raises(ValidationError):
        await exec_.execute_node(_node(), {"input": "text"}, "ws-1")


@pytest.mark.asyncio
async def test_a13_failed_status_maps_to_failure() -> None:
    from workflow_engine.adapters.v3_adapter import V3ContractError, V3NodeExecutor

    async def spy(request):
        return {"status": "failed", "error": {"code": "provider_error", "message": "boom"}}

    exec_ = V3NodeExecutor(
        agent_runner=spy,
        manifest=CapabilityManifest(enabled_capabilities={"agent.run"}),
        enabled=True,
    )
    with __import__("pytest").raises(V3ContractError):
        await exec_.execute_node(_node(), {"input": "text"}, "ws-1")


@pytest.mark.asyncio
async def test_a13_cancelled_maps_to_cancelled() -> None:
    import asyncio as _asyncio

    from workflow_engine.adapters.v3_adapter import V3NodeExecutor

    async def spy(request):
        return {
            "status": "failed",
            "cancelled": True,
            "error": {"code": "cancelled", "message": "stopped"},
        }

    exec_ = V3NodeExecutor(
        agent_runner=spy,
        manifest=CapabilityManifest(enabled_capabilities={"agent.run"}),
        enabled=True,
    )
    with __import__("pytest").raises(_asyncio.CancelledError):
        await exec_.execute_node(_node(), {"input": "text"}, "ws-1")
