"""W2 fixture test: restricted Langflow round-trip over the reference fixture.

The fixture ``tests/fixtures/workflows/langflow_restricted_roundtrip.json`` is
the single reviewable artifact that defines the *restricted authoring subset*
the Langflow spike supports.  This module asserts the three properties the W2
card requires of it:

1. **Security**: it imports without a single rejection — no custom code
   component, no executable-code marker, no unvetted module path.
2. **Contract**: it compiles to a deterministic ``ExecutionPlan`` through the
   W0/W1 validator and compiler with zero tool/model calls.
3. **Round-trip purity**: ``Langflow JSON -> WorkflowRevision -> Langflow
   JSON`` reproduces the same content hash and node/edge specs.

The fixture is deliberately free of credentials, absolute paths, PHI and
custom components, so this test also acts as a hygiene check on anything
checked in under ``tests/fixtures/workflows/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from workflow_engine import (
    CapabilityManifest,
    LangflowAdapter,
    LangflowImportError,
    NodeSpec,
    NodeType,
    WorkflowCompiler,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowValidator,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "workflows"
REFERENCE_FLOW = FIXTURES_DIR / "langflow_restricted_roundtrip.json"

WORKFLOW_ID = "wf-reference"
WORKSPACE_ID = "ws-reference"

#: Capabilities the reference flow needs (LLM) — the fixture's declared model
#: must be vetted for the import to succeed at all.
MANIFEST = CapabilityManifest(
    supported_node_types=set(NodeType),
    enabled_capabilities={"llm"},
)


def _load_fixture() -> dict[str, Any]:
    """Load the reference flow JSON from disk."""
    assert REFERENCE_FLOW.exists(), f"missing fixture: {REFERENCE_FLOW}"
    return json.loads(REFERENCE_FLOW.read_text())


def _import_revision(flow: dict[str, Any]) -> WorkflowRevision:
    return LangflowAdapter.import_revision(
        flow, workflow_id=WORKFLOW_ID, workspace_id=WORKSPACE_ID
    )


# ---------------------------------------------------------------------------
# Fixture hygiene (checked-in artifact must stay reviewable)
# ---------------------------------------------------------------------------


class TestFixtureHygiene:
    """The fixture must remain a safe, self-describing reviewable artifact."""

    def test_fixture_exists_and_is_json(self) -> None:
        flow = _load_fixture()
        assert isinstance(flow, dict)
        assert "data" in flow

    def test_fixture_has_no_executable_code_markers(self) -> None:
        """No key anywhere in the fixture may carry an executable-code marker."""
        from workflow_engine.adapters.langflow_adapter import _CODE_FIELD_MARKERS

        found: set[str] = set()

        def _walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, sub in value.items():
                    if isinstance(key, str) and key.lower() in _CODE_FIELD_MARKERS:
                        found.add(key.lower())
                    _walk(sub)
            elif isinstance(value, list):
                for sub in value:
                    _walk(sub)

        _walk(_load_fixture())
        assert not found, f"fixture carries code marker(s): {sorted(found)}"

    def test_fixture_components_are_all_allowlisted(self) -> None:
        flow = _load_fixture()
        components = [n["data"]["type"] for n in flow["data"]["nodes"]]
        assert components
        for component in components:
            assert LangflowAdapter.is_component_supported(component), component

    def test_fixture_has_no_absolute_paths_or_credentials(self) -> None:
        raw = REFERENCE_FLOW.read_text()
        assert "/root/" not in raw
        assert "api_key" not in raw.lower().replace("_comment", "")
        assert "sk-" not in raw

    def test_fixture_records_pin_metadata(self) -> None:
        flow = _load_fixture()
        assert "langflow" in json.dumps(flow.get("_comment", "")).lower()


# ---------------------------------------------------------------------------
# Security pass
# ---------------------------------------------------------------------------


class TestReferenceFlowPassesSecurity:
    """The restricted subset must import without any rejection."""

    def test_import_definition_succeeds(self) -> None:
        definition = LangflowAdapter.import_definition(
            _load_fixture(),
            workflow_id=WORKFLOW_ID,
            workspace_id=WORKSPACE_ID,
        )
        assert isinstance(definition, WorkflowDefinition)
        assert definition.id == WORKFLOW_ID
        assert definition.workspace_id == WORKSPACE_ID

    def test_node_types_map_to_expected_product_types(self) -> None:
        definition = LangflowAdapter.import_definition(
            _load_fixture(),
            workflow_id=WORKFLOW_ID,
            workspace_id=WORKSPACE_ID,
        )
        by_id = {n.id: n for n in definition.nodes}
        assert by_id["ChatInput-ref01"].type == NodeType.TRIGGER
        assert by_id["ChatOpenAI-ref02"].type == NodeType.LLM
        assert by_id["ConditionalRouter-ref03"].type == NodeType.CONDITION
        assert by_id["ChatOutput-ref04"].type == NodeType.OUTPUT
        assert by_id["TextOutput-ref05"].type == NodeType.OUTPUT

    def test_nodes_have_typed_ports(self) -> None:
        definition = LangflowAdapter.import_definition(
            _load_fixture(),
            workflow_id=WORKFLOW_ID,
            workspace_id=WORKSPACE_ID,
        )
        for node in definition.nodes:
            assert isinstance(node, NodeSpec)
            assert node.input_ports or node.output_ports

    def test_edges_map_to_port_level_specs(self) -> None:
        definition = LangflowAdapter.import_definition(
            _load_fixture(),
            workflow_id=WORKFLOW_ID,
            workspace_id=WORKSPACE_ID,
        )
        assert len(definition.edges) == 4

    def test_condition_routing_values_preserved(self) -> None:
        definition = LangflowAdapter.import_definition(
            _load_fixture(),
            workflow_id=WORKFLOW_ID,
            workspace_id=WORKSPACE_ID,
        )
        cond_edges = [
            e for e in definition.edges
            if e.source_node_id == "ConditionalRouter-ref03"
        ]
        assert len(cond_edges) == 2
        assert {e.condition_value for e in cond_edges} == {True, False}

    def test_llm_config_is_vetted_and_bounded(self) -> None:
        from workflow_engine.adapters.langflow_adapter import VETTED_MODELS

        definition = LangflowAdapter.import_definition(
            _load_fixture(),
            workflow_id=WORKFLOW_ID,
            workspace_id=WORKSPACE_ID,
        )
        llm = next(n for n in definition.nodes if n.id == "ChatOpenAI-ref02")
        assert llm.config["model"] in VETTED_MODELS
        assert llm.config["temperature"] == 0.2
        assert llm.config["max_tokens"] == 256
        assert len(str(llm.config)) < 2000

    def test_no_capability_can_be_widened_by_the_flow(self) -> None:
        """Capabilities come from the catalog, never from the imported JSON."""
        definition = LangflowAdapter.import_definition(
            _load_fixture(),
            workflow_id=WORKFLOW_ID,
            workspace_id=WORKSPACE_ID,
        )
        catalog_caps = {
            cap
            for spec in LangflowAdapter.catalog().values()
            for cap in spec.required_capabilities
        }
        for node in definition.nodes:
            assert set(node.required_capabilities) <= catalog_caps


# ---------------------------------------------------------------------------
# Contract: validate + compile (no tool/model calls)
# ---------------------------------------------------------------------------


class TestReferenceFlowCompiles:
    """The imported revision must pass W0/W1 validation and compilation."""

    def _import(self) -> WorkflowRevision:
        return _import_revision(_load_fixture())

    def test_validator_accepts_imported_definition(self) -> None:
        revision = self._import()
        result = WorkflowValidator.validate(revision.spec, MANIFEST)
        assert result.valid, result.error_summary

    def test_compiler_produces_deterministic_plan(self) -> None:
        revision = self._import()
        plan1 = WorkflowCompiler.compile(revision, manifest=MANIFEST)
        plan2 = WorkflowCompiler.compile(revision, manifest=MANIFEST)
        assert plan1.steps == plan2.steps
        assert plan1.revision_hash == revision.content_hash

    def test_plan_orders_trigger_before_outputs(self) -> None:
        revision = self._import()
        plan = WorkflowCompiler.compile(revision, manifest=MANIFEST)
        order = {s.node_id: s.order for s in plan.steps}
        assert order["ChatInput-ref01"] == 0
        assert order["ConditionalRouter-ref03"] < order["ChatOutput-ref04"]
        assert order["ConditionalRouter-ref03"] < order["TextOutput-ref05"]

    def test_condition_downstream_single_inbound_trivial_all_barrier(self) -> None:
        """T-V4-008: single-inbound downstream nodes carry the trivial ``all``
        barrier (join modes are declared, not inferred from the source type);
        the runtime behaviour for one inbound edge is unchanged."""
        revision = self._import()
        plan = WorkflowCompiler.compile(revision, manifest=MANIFEST)
        for node_id in ("ChatOutput-ref04", "TextOutput-ref05"):
            step = plan.step_by_node_id(node_id)
            assert step is not None
            assert step.join_mode == "all"
            assert step.join_waits_for == ["input_value"]
            assert step.join_emit_on_partial == "nothing"
            assert step.join_timeout_s is not None

    def test_rejected_when_capability_disabled(self) -> None:
        """Same fixture with 'llm' disabled must fail honestly, not pass."""
        revision = self._import()
        strict = CapabilityManifest(
            supported_node_types=set(NodeType),
            enabled_capabilities=set(),
        )
        result = WorkflowValidator.validate(revision.spec, strict)
        assert not result.valid
        codes = {e.code for e in result.errors}
        assert "UNSUPPORTED_CAPABILITY" in codes

    def test_revision_is_immutable(self) -> None:
        revision = self._import()
        original = revision.model_dump(mode="json")
        with pytest.raises(ValidationError):
            revision.revision = 2  # type: ignore[misc]  # frozen by design
        assert revision.model_dump(mode="json") == original


# ---------------------------------------------------------------------------
# Round-trip purity
# ---------------------------------------------------------------------------


class TestReferenceFlowRoundTrips:
    """Langflow JSON -> WorkflowRevision -> Langflow JSON must be lossless."""

    def _revision(self) -> WorkflowRevision:
        return _import_revision(_load_fixture())

    def test_round_trip_content_hash_equal(self) -> None:
        revision = self._revision()
        exported = LangflowAdapter.export_revision(revision)
        reimported = _import_revision(exported)
        assert reimported.content_hash == revision.content_hash

    def test_round_trip_node_specs_equal(self) -> None:
        revision = self._revision()
        exported = LangflowAdapter.export_definition(revision.spec)
        reimported = LangflowAdapter.import_definition(
            exported, workflow_id=WORKFLOW_ID, workspace_id=WORKSPACE_ID
        )
        assert reimported.nodes == revision.spec.nodes
        assert reimported.edges == revision.spec.edges

    def test_exported_flow_is_reimportable_and_compiles(self) -> None:
        revision = self._revision()
        exported = LangflowAdapter.export_revision(revision)
        reimported = _import_revision(exported)
        result = WorkflowValidator.validate(reimported.spec, MANIFEST)
        assert result.valid, result.error_summary
        plan = WorkflowCompiler.compile(reimported, manifest=MANIFEST)
        assert len(plan.steps) == len(revision.spec.nodes)

    def test_export_carries_revision_metadata(self) -> None:
        revision = self._revision()
        exported = LangflowAdapter.export_revision(revision)
        assert exported["data"]["revision"] == revision.revision
        assert exported["data"]["revision_hash"] == revision.content_hash

    def test_export_contains_no_secrets_or_code_fields(self) -> None:
        revision = self._revision()
        exported = LangflowAdapter.export_revision(revision)
        lowered = json.dumps(exported).lower()
        assert "api_key" not in lowered
        assert "sk-" not in lowered
        assert "/root/" not in lowered

        # No exported key may be an executable-code field name.
        def _walk_keys(value: Any) -> set[str]:
            keys: set[str] = set()
            if isinstance(value, dict):
                for key, sub in value.items():
                    if isinstance(key, str):
                        keys.add(key.lower())
                    keys |= _walk_keys(sub)
            elif isinstance(value, list):
                for sub in value:
                    keys |= _walk_keys(sub)
            return keys

        assert not (_walk_keys(exported) & {"code", "source_code", "python_code"})


# ---------------------------------------------------------------------------
# Rejection of out-of-subset flows (negative evidence)
# ---------------------------------------------------------------------------


class TestOutOfSubsetFlowsRejected:
    """Anything outside the restricted subset must still be fail-closed."""

    def test_custom_code_component_still_rejected(self) -> None:
        flow = _load_fixture()
        flow["data"]["nodes"].append({
            "id": "Custom-ref06",
            "type": "genericNode",
            "data": {"type": "Code", "node": {"template": {"code": {"value": "x = 1"}}}},
        })
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id=WORKFLOW_ID, workspace_id=WORKSPACE_ID
            )

    def test_unvetted_module_path_still_rejected(self) -> None:
        flow = _load_fixture()
        flow["data"]["nodes"].append({
            "id": "ext-ref06",
            "type": "genericNode",
            "data": {"type": "ext:rogue:Rogue", "node": {"template": {}}},
        })
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id=WORKFLOW_ID, workspace_id=WORKSPACE_ID
            )

    def test_unknown_component_still_rejected(self) -> None:
        flow = _load_fixture()
        flow["data"]["nodes"].append({
            "id": "rogue-ref06",
            "type": "genericNode",
            "data": {"type": "NotARealComponent", "node": {"template": {}}},
        })
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id=WORKFLOW_ID, workspace_id=WORKSPACE_ID
            )

    def test_tampered_node_id_is_rejected(self) -> None:
        flow = _load_fixture()
        flow["data"]["nodes"][0]["id"] = flow["data"]["nodes"][0]["id"] + "-tampered"
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id=WORKFLOW_ID, workspace_id=WORKSPACE_ID
            )
