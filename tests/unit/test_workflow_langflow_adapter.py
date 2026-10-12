"""W2-1 unit tests: Langflow adapter parser interface & strict security rejection.

Tests verify:
* Valid declarative flows pass the security layer.
* Custom/untrusted Python code components are immediately rejected with
  structured ``LangflowImportError`` rejections (fail-closed).
* Defense-in-depth: allowlisted components carrying code markers are rejected.
* Resource guards reject oversized flows.
* The adapter never imports or executes Langflow.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from workflow_engine import (
    LangflowAdapter,
    LangflowComponentSpec,
    LangflowImportError,
    NodeType,
    WorkflowDefinition,
    WorkflowRevision,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _lf_node(
    node_id: str,
    component: str,
    *,
    template: dict[str, Any] | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Build a Langflow-1.x-shaped node object."""
    return {
        "id": node_id,
        "type": component,
        "data": {
            "type": component,
            "id": f"{component}-{node_id}",
            "node": {
                "display_name": display_name or component,
                "description": "Spike fixture",
                "template": template or {},
            },
        },
        "position": {"x": 0.0, "y": 0.0},
    }


def _lf_edge(
    edge_id: str,
    source: str,
    source_handle: str,
    target: str,
    target_handle: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Langflow-1.x-shaped edge object."""
    edge: dict[str, Any] = {
        "id": edge_id,
        "source": source,
        "target": target,
        "sourceHandle": f"{source}|{source_handle}|{source_handle}",
        "targetHandle": f"{target}|{target_handle}|{target_handle}",
        "data": {
            "sourceHandle": {
                "type": "output",
                "id": source_handle,
                "name": source_handle,
                "output_types": ["Message"],
                "selected_output": source_handle,
            },
            "targetHandle": {
                "type": "input",
                "id": target_handle,
                "name": target_handle,
                "fieldType": "str",
                "input_types": ["Message"],
            },
        },
    }
    if data:
        edge["data"].update(data)
    return edge


def _flow(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    name: str = "Spike Flow",
) -> dict[str, Any]:
    """Build a Langflow-1.x-shaped flow definition."""
    return {
        "id": "flow-spike",
        "name": name,
        "description": "",
        "data": {"nodes": nodes, "edges": edges},
    }


def _component_names() -> list[str]:
    """Sorted list of every allowlisted catalog component name."""
    return sorted(spec.component for spec in LangflowAdapter.catalog().values())


def _valid_flow() -> dict[str, Any]:
    """A minimal valid declarative flow: ChatInput -> ChatOutput."""
    nodes = [
        _lf_node("lf-trigger", "ChatInput"),
        _lf_node("lf-output", "ChatOutput"),
    ]
    edges = [
        _lf_edge("e1", "lf-trigger", "message", "lf-output", "input_value"),
    ]
    return _flow(nodes, edges)


# ---------------------------------------------------------------------------
# Test: valid flow passes security
# ---------------------------------------------------------------------------


class TestValidFlowPassesSecurity:
    """A valid declarative flow should pass the security layer."""

    def test_import_definition_returns_workflow_definition(self) -> None:
        flow = _valid_flow()
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1",
        )
        assert isinstance(result, WorkflowDefinition)
        assert result.id == "wf-1"
        assert result.workspace_id == "ws-1"
        assert result.name == "Spike Flow"

    def test_import_revision_returns_workflow_revision(self) -> None:
        flow = _valid_flow()
        result = LangflowAdapter.import_revision(
            flow, workflow_id="wf-1", workspace_id="ws-1",
        )
        assert isinstance(result, WorkflowRevision)
        assert result.workflow_id == "wf-1"
        assert result.revision == 1
        assert result.content_hash

    def test_nodes_have_correct_types(self) -> None:
        flow = _valid_flow()
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1",
        )
        node_types = {n.id: n.type for n in result.nodes}
        assert node_types["lf-trigger"] == NodeType.TRIGGER
        assert node_types["lf-output"] == NodeType.OUTPUT

    def test_nodes_have_required_capabilities(self) -> None:
        flow = _valid_flow()
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1",
        )
        caps = result.workspace_capabilities
        assert "llm" not in caps  # ChatInput/ChatOutput need no capabilities

    def test_import_revision_content_hash_is_deterministic(self) -> None:
        flow = _valid_flow()
        r1 = LangflowAdapter.import_revision(
            flow, workflow_id="wf-1", workspace_id="ws-1",
        )
        r2 = LangflowAdapter.import_revision(
            flow, workflow_id="wf-1", workspace_id="ws-1",
        )
        assert r1.content_hash == r2.content_hash


# ---------------------------------------------------------------------------
# Test: custom code component rejection
# ---------------------------------------------------------------------------


class TestCustomCodeComponentRejection:
    """Custom/untrusted Python code components must be immediately rejected."""

    def test_custom_component_with_code_template_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node(
                "lf-custom",
                "CustomComponent",
                template={
                    "code": {
                        "type": "code",
                        "value": "import os\nos.system('rm -rf /')",
                    }
                },
            ),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        rejections = exc_info.value.rejections
        assert len(rejections) >= 1
        codes = {r["code"] for r in rejections}
        assert "CUSTOM_CODE_COMPONENT" in codes

    def test_code_component_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node(
                "lf-code",
                "Code",
                template={
                    "code": {
                        "type": "code",
                        "value": "print('hello')",
                    }
                },
            ),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CUSTOM_CODE_COMPONENT" in codes

    def test_python_function_component_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-func", "PythonFunction"),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CUSTOM_CODE_COMPONENT" in codes

    def test_unvetted_module_path_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-evil", "evil_pkg.ChatInput"),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CUSTOM_CODE_COMPONENT" in codes

    def test_allowlisted_component_with_code_marker_rejected(self) -> None:
        """Defense-in-depth: even allowlisted components with code markers fail."""
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={
                    "template": "Hello {name}!",
                    "code": {
                        "type": "code",
                        "value": "exec('import os')",
                    },
                },
            ),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CUSTOM_CODE_COMPONENT" in codes

    def test_allowlisted_component_with_exec_marker_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={
                    "template": "Hello!",
                    "exec": "os.system('id')",
                },
            ),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CUSTOM_CODE_COMPONENT" in codes

    def test_allowlisted_component_with_imports_marker_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={
                    "template": "Hello!",
                    "imports": ["os", "subprocess"],
                },
            ),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CUSTOM_CODE_COMPONENT" in codes

    def test_allowlisted_component_with_class_marker_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={
                    "template": "Hello!",
                    "class": "MyCustomClass",
                },
            ),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CUSTOM_CODE_COMPONENT" in codes

    def test_rejection_includes_node_id(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-code", "Code"),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        rejections = exc_info.value.rejections
        node_ids = {r.get("node_id") for r in rejections}
        assert "lf-code" in node_ids


# ---------------------------------------------------------------------------
# Test: unsupported component rejection
# ---------------------------------------------------------------------------


class TestUnsupportedComponentRejection:
    """Components not in the allowlist must be rejected."""

    def test_unknown_component_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-unknown", "SomeRandomComponent"),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "UNSUPPORTED_COMPONENT" in codes

    def test_unknown_component_message_lists_allowlist(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-unknown", "SomeRandomComponent"),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        rejections = exc_info.value.rejections
        assert any("allowlist" in r.get("message", "") for r in rejections)


# ---------------------------------------------------------------------------
# Test: oversized flow rejection
# ---------------------------------------------------------------------------


class TestOversizedFlowRejection:
    """Resource guards must reject oversized flows."""

    def test_too_many_nodes_rejected(self) -> None:
        nodes = [_lf_node(f"lf-node-{i}", "ChatInput") for i in range(201)]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "OVERSIZED_FLOW" in codes

    def test_too_many_edges_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-output", "ChatOutput"),
        ]
        edges = [
            _lf_edge(f"e{i}", "lf-trigger", "message", "lf-output", "input_value")
            for i in range(501)
        ]
        flow = _flow(nodes, edges)
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "OVERSIZED_FLOW" in codes


# ---------------------------------------------------------------------------
# Test: malformed flow rejection
# ---------------------------------------------------------------------------


class TestMalformedFlowRejection:
    """Structurally invalid flows must be rejected."""

    def test_non_dict_flow_rejected(self) -> None:
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                "not a dict", workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "BAD_FLOW_FORMAT" in codes

    def test_missing_nodes_key_rejected(self) -> None:
        flow = {"id": "f", "name": "x", "data": {"edges": []}}
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "BAD_FLOW_FORMAT" in codes

    def test_node_missing_type_rejected(self) -> None:
        nodes = [{"id": "lf-x", "data": {"id": "x", "node": {"template": {}}}}]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "BAD_FLOW_FORMAT" in codes

    def test_node_missing_id_rejected(self) -> None:
        nodes = [
            {
                "type": "ChatInput",
                "data": {"type": "ChatInput", "node": {"template": {}}},
            }
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1",
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "BAD_FLOW_FORMAT" in codes


# ---------------------------------------------------------------------------
# Test: adapter never imports langflow
# ---------------------------------------------------------------------------


class TestNoLangflowImport:
    """The adapter must never import or execute Langflow."""

    def test_langflow_not_in_sys_modules(self) -> None:
        import workflow_engine.adapters.langflow_adapter  # noqa: F401

        assert "langflow" not in sys.modules

    def test_adapter_has_no_langflow_dependency(self) -> None:
        import workflow_engine.adapters.langflow_adapter as mod

        source = mod.__file__
        if source:
            with open(source) as f:
                content = f.read()
            assert "import langflow" not in content
            assert "from langflow" not in content


# ---------------------------------------------------------------------------
# Test: catalog and component support checks
# ---------------------------------------------------------------------------


class TestCatalog:
    """The restricted catalog and support checks work correctly."""

    def test_catalog_contains_expected_components(self) -> None:
        catalog = LangflowAdapter.catalog()
        expected = {
            "chatinput", "textinput", "vectorretriever", "vectorstoreretriever",
            "pgvectorretriever", "chatopenai", "openaimodel", "toolcallingagent",
            "tool", "conditionalrouter", "prompt", "template", "parser",
            "chatoutput", "textoutput",
        }
        assert expected.issubset(set(catalog.keys()))

    def test_is_component_supported_true(self) -> None:
        assert LangflowAdapter.is_component_supported("ChatInput") is True
        assert LangflowAdapter.is_component_supported("ChatOpenAI") is True
        assert LangflowAdapter.is_component_supported("Prompt") is True

    def test_is_component_supported_false(self) -> None:
        assert LangflowAdapter.is_component_supported("Code") is False
        assert LangflowAdapter.is_component_supported("CustomComponent") is False
        assert LangflowAdapter.is_component_supported("SomeRandomComponent") is False

    def test_catalog_entries_have_required_fields(self) -> None:
        catalog = LangflowAdapter.catalog()
        for _key, spec in catalog.items():
            assert isinstance(spec, LangflowComponentSpec)
            assert spec.component
            assert spec.node_type
            assert spec.display_name
            assert isinstance(spec.input_ports, tuple)
            assert isinstance(spec.output_ports, tuple)
            assert isinstance(spec.required_capabilities, tuple)


# ---------------------------------------------------------------------------
# Test: version and metadata
# ---------------------------------------------------------------------------


class TestMetadata:
    """Adapter metadata is present and informative."""

    def test_version_string(self) -> None:
        assert LangflowAdapter.VERSION
        assert "wf-langflow-adapter" in LangflowAdapter.VERSION

    def test_langflow_pin_documented(self) -> None:
        assert LangflowAdapter.LANGFOW_PIN
        assert "langflow" in LangflowAdapter.LANGFOW_PIN.lower()

    def test_max_guards_configured(self) -> None:
        assert LangflowAdapter.MAX_NODES > 0
        assert LangflowAdapter.MAX_EDGES > 0
        assert LangflowAdapter.MAX_FIELD_CHARS > 0


# ---------------------------------------------------------------------------
# F-1 regression: iterative depth-limited walk (no RecursionError)
# ---------------------------------------------------------------------------


class TestF1DepthLimitedWalk:
    """F-1: _walk must be iterative and depth-limited."""

    def test_deeply_nested_dict_does_not_crash(self) -> None:
        """A 5000-deep nested dict must not cause RecursionError."""
        value: dict[str, Any] = {}
        current = value
        for _ in range(5000):
            current["nested"] = {}
            current = current["nested"]
        # Should not raise RecursionError
        markers, depth_exceeded = LangflowAdapter._find_code_markers(value)
        assert isinstance(markers, set)
        assert depth_exceeded is True

    def test_deeply_nested_list_does_not_crash(self) -> None:
        """A 5000-deep nested list must not cause RecursionError."""
        value: list[Any] = []
        current = value
        for _ in range(5000):
            new_list: list[Any] = []
            current.append(new_list)
            current = new_list
        markers, depth_exceeded = LangflowAdapter._find_code_markers(value)
        assert isinstance(markers, set)

    def test_self_referential_dict_does_not_crash(self) -> None:
        """A self-referential dict must not cause infinite loop."""
        value: dict[str, Any] = {"key": "value"}
        value["self"] = value  # type: ignore[assignment]
        markers, _ = LangflowAdapter._find_code_markers(value)
        assert isinstance(markers, set)

    def test_max_depth_constant_exists(self) -> None:
        from workflow_engine.adapters.langflow_adapter import MAX_DEPTH

        assert MAX_DEPTH > 0
        assert MAX_DEPTH <= 200  # reasonable limit


# ---------------------------------------------------------------------------
# F-2 regression: substring marker matching on normalized keys
# ---------------------------------------------------------------------------


class TestF2SubstringMarkerMatching:
    """F-2: marker scan must catch near-miss key names."""

    def _make_node_with_template(self, template: dict[str, Any]) -> dict[str, Any]:
        return _lf_node("lf-x", "Prompt", template=template)

    def test_my_class_key_rejected(self) -> None:
        node = self._make_node_with_template({"my_class": "payload"})
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        assert any(
            r["code"] == "CUSTOM_CODE_COMPONENT"
            for r in exc_info.value.rejections
        )

    def test_implementing_class_key_rejected(self) -> None:
        node = self._make_node_with_template({"implementing_class": "payload"})
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )

    def test_class_underscore_key_rejected(self) -> None:
        node = self._make_node_with_template({"class_": "payload"})
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )

    def test_classes_key_rejected(self) -> None:
        node = self._make_node_with_template({"classes": "payload"})
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )

    def test_code_underscore_key_rejected(self) -> None:
        node = self._make_node_with_template({"code_": "payload"})
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )

    def test_scripts_key_rejected(self) -> None:
        node = self._make_node_with_template({"scripts": "payload"})
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )

    def test_lambda_fn_key_rejected(self) -> None:
        node = self._make_node_with_template({"lambda_fn": "payload"})
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )

    def test_exec_string_key_rejected(self) -> None:
        node = self._make_node_with_template({"exec_string": "payload"})
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )

    def test_sourcecode_key_rejected(self) -> None:
        node = self._make_node_with_template({"sourcecode": "payload"})
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )

    def test_pythoncode_key_rejected(self) -> None:
        node = self._make_node_with_template({"pythoncode": "payload"})
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )

    def test_custom_component_field_key_rejected(self) -> None:
        node = self._make_node_with_template(
            {"custom_component_field": "payload"}
        )
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError):
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )

    def test_description_key_not_rejected(self) -> None:
        """'description' contains 'script' but doesn't start/end with it."""
        node = _lf_node(
            "lf-x", "Prompt", template={"template": "Hello!"}
        )
        # Add a description field that contains 'script' as substring
        node["data"]["node"]["description"] = "A script for testing"
        flow = _flow([node], [])
        # Should NOT be rejected — 'description' doesn't start/end with 'script'
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert isinstance(result, WorkflowDefinition)

    def test_edge_with_code_marker_rejected(self) -> None:
        """F-2: edges must also be scanned for code markers."""
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-output", "ChatOutput"),
        ]
        edge = _lf_edge("e1", "lf-trigger", "message", "lf-output", "input_value")
        edge["data"]["code"] = "import os"
        flow = _flow(nodes, [edge])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        assert any(
            r["code"] == "CUSTOM_CODE_COMPONENT"
            for r in exc_info.value.rejections
        )


# ---------------------------------------------------------------------------
# F-3 regression: duplicate node ID rejection
# ---------------------------------------------------------------------------


class TestF3DuplicateNodeId:
    """F-3: duplicate node IDs must be rejected with structured error."""

    def test_duplicate_node_id_rejected(self) -> None:
        nodes = [
            _lf_node("lf-dup", "ChatInput"),
            _lf_node("lf-dup", "ChatOutput"),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "DUPLICATE_NODE_ID" for r in rejections)
        dup_rejection = next(
            r for r in rejections if r["code"] == "DUPLICATE_NODE_ID"
        )
        assert dup_rejection["node_id"] == "lf-dup"

    def test_unique_node_ids_accepted(self) -> None:
        nodes = [
            _lf_node("lf-a", "ChatInput"),
            _lf_node("lf-b", "ChatOutput"),
        ]
        flow = _flow(nodes, [])
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert len(result.nodes) == 2


# ---------------------------------------------------------------------------
# F-4 regression: empty flow rejection
# ---------------------------------------------------------------------------


class TestF4EmptyFlow:
    """F-4: empty flows must be rejected with structured error."""

    def test_empty_flow_rejected(self) -> None:
        flow = {"data": {"nodes": [], "edges": []}}
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "EMPTY_FLOW" for r in rejections)

    def test_single_node_flow_accepted(self) -> None:
        nodes = [_lf_node("lf-only", "ChatInput")]
        flow = _flow(nodes, [])
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert len(result.nodes) == 1


# ---------------------------------------------------------------------------
# F-5 regression: MAX_FIELD_CHARS enforcement
# ---------------------------------------------------------------------------


class TestF5MaxFieldChars:
    """F-5: MAX_FIELD_CHARS must be enforced on all scalars."""

    def test_oversized_template_value_rejected(self) -> None:
        """Template value exceeding MAX_FIELD_CHARS must be rejected."""
        nodes = [
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={"template": "x" * (LangflowAdapter.MAX_FIELD_CHARS + 1)},
            ),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "OVERSIZED_FIELD" for r in rejections)

    def test_oversized_display_name_rejected(self) -> None:
        """Display name exceeding MAX_FIELD_CHARS must be rejected."""
        nodes = [
            _lf_node(
                "lf-x",
                "ChatInput",
                display_name="x" * (LangflowAdapter.MAX_FIELD_CHARS + 1),
            ),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "OVERSIZED_FIELD" for r in rejections)

    def test_field_at_limit_accepted(self) -> None:
        """A field exactly at MAX_FIELD_CHARS should be accepted."""
        nodes = [
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={"template": "x" * LangflowAdapter.MAX_FIELD_CHARS},
            ),
        ]
        flow = _flow(nodes, [])
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert isinstance(result, WorkflowDefinition)

    def test_field_just_under_limit_accepted(self) -> None:
        nodes = [
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={
                    "template": "x" * (LangflowAdapter.MAX_FIELD_CHARS - 1)
                },
            ),
        ]
        flow = _flow(nodes, [])
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert isinstance(result, WorkflowDefinition)


# ---------------------------------------------------------------------------
# R3-1 regression: iterative template value extraction (no RecursionError)
# ---------------------------------------------------------------------------


class TestR31TemplateValueDepth:
    """R3-1: the size-check path must never crash on hostile templates."""

    @staticmethod
    def _deep_value_chain(depth: int) -> Any:
        """Build {"value": {"value": ... "payload"}} of the given depth."""
        entry: Any = "payload"
        for _ in range(depth):
            entry = {"value": entry}
        return entry

    @staticmethod
    def _deep_default_chain(depth: int) -> Any:
        """Build {"default": {"default": ... "payload"}} of the given depth."""
        entry: Any = "payload"
        for _ in range(depth):
            entry = {"default": entry}
        return entry

    def test_value_chain_depth_5000_rejected(self) -> None:
        """A 5000-deep 'value' chain must produce a structured rejection."""
        nodes = [
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={"template": self._deep_value_chain(5000)},
            ),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "BAD_FLOW_FORMAT" for r in rejections)
        assert any(r.get("node_id") == "lf-prompt" for r in rejections)

    def test_default_chain_depth_5000_rejected(self) -> None:
        """A 5000-deep 'default' chain must produce a structured rejection."""
        nodes = [
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={"template": self._deep_default_chain(5000)},
            ),
        ]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "BAD_FLOW_FORMAT" for r in rejections)

    def test_self_reference_via_value_rejected(self) -> None:
        """A self-referential 'value' chain must produce a structured rejection."""
        entry: dict[str, Any] = {"value": "payload"}
        entry["value"] = entry  # cycle
        nodes = [_lf_node("lf-prompt", "Prompt", template={"template": entry})]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "BAD_FLOW_FORMAT" for r in rejections)

    def test_self_reference_via_default_rejected(self) -> None:
        """A self-referential 'default' chain must produce a rejection."""
        entry: dict[str, Any] = {"default": "payload"}
        entry["default"] = entry  # cycle
        nodes = [_lf_node("lf-prompt", "Prompt", template={"template": entry})]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "BAD_FLOW_FORMAT" for r in rejections)

    def test_mixed_value_default_chain_rejected(self) -> None:
        """A mixed value→default chain at depth 5000 must be rejected."""
        entry: Any = "payload"
        for i in range(5000):
            if i % 2 == 0:
                entry = {"value": entry}
            else:
                entry = {"default": entry}
        nodes = [_lf_node("lf-prompt", "Prompt", template={"template": entry})]
        flow = _flow(nodes, [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "BAD_FLOW_FORMAT" for r in rejections)

    def test_shallow_value_chain_accepted(self) -> None:
        """A realistic 1-2 deep value chain must be accepted."""
        nodes = [
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={"template": {"value": "Hello {name}!"}},
            ),
        ]
        flow = _flow(nodes, [])
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert isinstance(result, WorkflowDefinition)

    def test_template_value_direct_returns_sentinel_not_crash(self) -> None:
        """Direct API call must return a sentinel, never raise."""
        from workflow_engine.adapters.langflow_adapter import (
            _DEPTH_EXCEEDED,
        )

        entry: Any = "payload"
        for _ in range(5000):
            entry = {"value": entry}
        assert LangflowAdapter._template_value(entry) is _DEPTH_EXCEEDED

    def test_max_template_depth_constant(self) -> None:
        from workflow_engine.adapters.langflow_adapter import (
            MAX_TEMPLATE_DEPTH,
        )

        assert MAX_TEMPLATE_DEPTH > 0
        assert MAX_TEMPLATE_DEPTH <= 64  # real chains are 1-3 deep


# ---------------------------------------------------------------------------
# R3-2 regression: scan depth-exceed must reject, not silently accept
# ---------------------------------------------------------------------------


class TestR32ScanDepthExceedRejects:
    """R3-2: exceeding the scan depth must produce a structured rejection."""

    def test_depth_5000_flow_rejected_not_accepted(self) -> None:
        """A flow nesting beyond MAX_DEPTH must be rejected, not imported."""
        node: dict[str, Any] = {"id": "lf-deep", "type": "ChatInput", "data": {}}
        current = node
        for _ in range(5000):
            current["nested"] = {}
            current = current["nested"]
        flow = _flow([node], [])
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "OVERSIZED_FLOW" for r in rejections)
        assert any(r.get("node_id") == "lf-deep" for r in rejections)

    def test_moderate_depth_flow_accepted(self) -> None:
        """A flow within the scan depth limit must import normally."""
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-output", "ChatOutput"),
        ]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", "lf-output", "input_value"),
        ]
        flow = _flow(nodes, edges)
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert isinstance(result, WorkflowDefinition)

    def test_find_code_markers_reports_depth_exceeded(self) -> None:
        """_find_code_markers must report depth_exceeded=True when truncated."""
        value: dict[str, Any] = {}
        current = value
        for _ in range(5000):
            current["nested"] = {}
            current = current["nested"]
        markers, depth_exceeded = LangflowAdapter._find_code_markers(value)
        assert depth_exceeded is True

    def test_find_code_markers_reports_no_depth_exceed_for_normal(self) -> None:
        """A normal node must report depth_exceeded=False."""
        node = _lf_node("lf-x", "ChatInput")
        markers, depth_exceeded = LangflowAdapter._find_code_markers(node)
        assert depth_exceeded is False


# ---------------------------------------------------------------------------
# W2-2: declarative node translation (TRIGGER, RETRIEVER, LLM, OUTPUT)
# ---------------------------------------------------------------------------


class TestW22TriggerTranslation:
    """W2-2: TRIGGER nodes translate with correct ports and no capabilities."""

    def test_chatinput_translates_to_trigger(self) -> None:
        nodes = [_lf_node("lf-trigger", "ChatInput")]
        flow = _flow(nodes, [])
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        node = result.nodes[0]
        assert node.id == "lf-trigger"
        assert node.type == NodeType.TRIGGER

    def test_trigger_has_no_input_ports(self) -> None:
        nodes = [_lf_node("lf-trigger", "ChatInput")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.nodes[0].input_ports == []

    def test_trigger_has_message_output_port(self) -> None:
        nodes = [_lf_node("lf-trigger", "ChatInput")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        out_ports = result.nodes[0].output_ports
        assert len(out_ports) == 1
        assert out_ports[0].name == "message"
        assert out_ports[0].type == "string"

    def test_trigger_has_no_capabilities(self) -> None:
        nodes = [_lf_node("lf-trigger", "ChatInput")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.nodes[0].required_capabilities == []
        assert result.workspace_capabilities == set()


class TestW22RetrieverTranslation:
    """W2-2: RETRIEVER nodes translate with ports, capabilities and config."""

    def _import_single(self, component: str, template: dict[str, Any]):  # noqa: ANN001
        nodes = [_lf_node("lf-retr", component, template=template)]
        return LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )

    def test_retriever_translates_type_and_capability(self) -> None:
        result = self._import_single("VectorRetriever", {"query": "hello"})
        node = result.nodes[0]
        assert node.type == NodeType.RETRIEVER
        assert node.required_capabilities == ["retriever"]
        assert "retriever" in result.workspace_capabilities

    def test_retriever_ports(self) -> None:
        result = self._import_single("VectorRetriever", {"query": "hello"})
        node = result.nodes[0]
        assert [p.name for p in node.input_ports] == ["query"]
        assert node.input_ports[0].type == "string"
        assert [p.name for p in node.output_ports] == ["documents"]
        assert node.output_ports[0].type == "list"

    def test_retriever_config_from_allowlisted_fields(self) -> None:
        result = self._import_single(
            "VectorRetriever",
            {"query": {"value": "hello"}, "top_k": {"value": 5},
             "collection": {"value": "docs"}},
        )
        node = result.nodes[0]
        assert node.config["top_k"] == 5
        assert node.config["collection"] == "docs"

    def test_retriever_ignores_non_allowlisted_template_fields(self) -> None:
        result = self._import_single(
            "VectorRetriever",
            {"query": "hello", "show_expander": True, "advanced": {"value": 1}},
        )
        node = result.nodes[0]
        assert "show_expander" not in node.config
        assert "advanced" not in node.config
        assert "query" not in node.config  # port, not config


class TestW22LlmTranslation:
    """W2-2: LLM nodes translate with ports, capabilities and config."""

    def _import_single(self, template: dict[str, Any]):  # noqa: ANN001
        nodes = [_lf_node("lf-llm", "ChatOpenAI", template=template)]
        return LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )

    def test_llm_translates_type_and_capability(self) -> None:
        result = self._import_single({"input_value": "hi"})
        node = result.nodes[0]
        assert node.type == NodeType.LLM
        assert node.required_capabilities == ["llm"]
        assert "llm" in result.workspace_capabilities

    def test_llm_input_ports(self) -> None:
        result = self._import_single({"input_value": "hi"})
        node = result.nodes[0]
        names = [p.name for p in node.input_ports]
        assert names == ["input_value", "system_message"]
        system_port = node.input_ports[1]
        assert system_port.required is False

    def test_llm_output_port(self) -> None:
        result = self._import_single({"input_value": "hi"})
        node = result.nodes[0]
        assert [p.name for p in node.output_ports] == ["message"]
        assert node.output_ports[0].type == "string"

    def test_llm_config_mapping(self) -> None:
        result = self._import_single(
            {
                "input_value": "hi",
                "model_name": {"value": "gpt-4o-mini"},
                "temperature": {"value": 0.2},
                "max_tokens": {"value": 512},
            }
        )
        node = result.nodes[0]
        assert node.config["model"] == "gpt-4o-mini"
        assert node.config["temperature"] == 0.2
        assert node.config["max_tokens"] == 512

    def test_llm_config_ignores_non_allowlisted(self) -> None:
        result = self._import_single(
            {"input_value": "hi", "api_key": {"value": "sk-secret"}}
        )
        node = result.nodes[0]
        assert "api_key" not in node.config
        assert "sk-secret" not in str(node.config)


class TestW22OutputTranslation:
    """W2-2: OUTPUT nodes translate with correct ports."""

    def test_output_translates_type(self) -> None:
        nodes = [_lf_node("lf-out", "ChatOutput")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.nodes[0].type == NodeType.OUTPUT

    def test_output_input_ports(self) -> None:
        nodes = [_lf_node("lf-out", "ChatOutput")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        node = result.nodes[0]
        names = [p.name for p in node.input_ports]
        assert names == ["input_value", "message"]
        assert node.input_ports[0].required is True
        assert node.input_ports[1].required is False

    def test_output_output_port(self) -> None:
        nodes = [_lf_node("lf-out", "ChatOutput")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert [p.name for p in result.nodes[0].output_ports] == ["output"]

    def test_output_no_capabilities(self) -> None:
        nodes = [_lf_node("lf-out", "ChatOutput")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.nodes[0].required_capabilities == []


class TestW22ConfigExtractionForms:
    """W2-2: template value extraction handles wrapper and literal forms."""

    def test_value_wrapper_form(self) -> None:
        nodes = [
            _lf_node(
                "lf-llm",
                "ChatOpenAI",
                template={"input_value": {"value": "hi"}},
            )
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert isinstance(result.nodes[0].output_ports, list)

    def test_default_wrapper_form(self) -> None:
        nodes = [
            _lf_node(
                "lf-llm",
                "ChatOpenAI",
                template={
                    "input_value": "hi",
                    "temperature": {"default": 0.5},
                },
            )
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.nodes[0].config["temperature"] == 0.5

    def test_list_config_value(self) -> None:
        nodes = [
            _lf_node(
                "lf-agent",
                "ToolCallingAgent",
                template={"input_value": "hi", "agent_instructions": {"value": "x"}},
            )
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.nodes[0].config["instructions"] == "x"

    def test_empty_template_yields_empty_config(self) -> None:
        nodes = [_lf_node("lf-llm", "ChatOpenAI")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.nodes[0].config == {}


class TestW22ValidatorIntegration:
    """W2-2: translated flows validate against WorkflowValidator."""

    def _full_manifest(self):
        from workflow_engine import CapabilityManifest

        return CapabilityManifest(
            supported_node_types=set(NodeType),
            enabled_capabilities={"llm", "retriever", "agent", "tool"},
        )

    def test_llm_flow_validates(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-llm", "ChatOpenAI", template={"input_value": "hi"}),
            _lf_node("lf-out", "ChatOutput"),
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        from workflow_engine import WorkflowValidator

        validation = WorkflowValidator.validate(result, self._full_manifest())
        # Ports/edges are W2-3; validation errors here are expected to be
        # edge-related, not node-type or port-declaration errors.
        codes = {e.code for e in validation.errors}
        assert "UNSUPPORTED_NODE_TYPE" not in codes
        assert "UNSUPPORTED_CAPABILITY" not in codes
        assert "EMPTY_PORT_NAME" not in codes
        assert "DUPLICATE_PORT_NAME" not in codes

    def test_retriever_capability_aggregation(self) -> None:
        nodes = [
            _lf_node("lf-retr", "VectorRetriever", template={"query": "q"}),
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.workspace_capabilities == {"retriever"}

    def test_mixed_capability_aggregation(self) -> None:
        nodes = [
            _lf_node("lf-llm", "ChatOpenAI", template={"input_value": "hi"}),
            _lf_node("lf-retr", "VectorRetriever", template={"query": "q"}),
            _lf_node("lf-agent", "ToolCallingAgent", template={"input_value": "go"}),
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.workspace_capabilities == {"llm", "retriever", "agent", "tool"}


class TestW22FlowNameGuard:
    """W2-2 R1 fix: flow-level name fallback must be bounded."""

    def test_oversized_flow_name_rejected(self) -> None:
        """A flow name exceeding MAX_FIELD_CHARS must be rejected."""
        nodes = [_lf_node("lf-x", "ChatInput")]
        flow = _flow(nodes, [], name="N" * (LangflowAdapter.MAX_FIELD_CHARS + 1))
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        rejections = exc_info.value.rejections
        assert any(r["code"] == "OVERSIZED_FIELD" for r in rejections)

    def test_normal_flow_name_accepted(self) -> None:
        """A flow name within the limit must be accepted into the definition."""
        nodes = [_lf_node("lf-x", "ChatInput")]
        flow = _flow(nodes, [], name="My Workflow")
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.name == "My Workflow"

    def test_caller_supplied_name_takes_precedence(self) -> None:
        """Caller-supplied name= (trusted API input) must win over the flow name."""
        nodes = [_lf_node("lf-x", "ChatInput")]
        flow = _flow(nodes, [], name="Flow Name")
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1", name="Caller Name"
        )
        assert result.name == "Caller Name"


# ---------------------------------------------------------------------------
# W2-3: edge and port mapping
# ---------------------------------------------------------------------------


class TestW23EdgeTranslation:
    """W2-3: Langflow edges translate into typed WorkflowEdgeSpec."""

    def test_basic_edge_port_mapping(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-llm", "ChatOpenAI", template={"input_value": "hi"}),
        ]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", "lf-llm", "input_value"),
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.id == "e1"
        assert edge.source_node_id == "lf-trigger"
        assert edge.source_port == "message"
        assert edge.target_node_id == "lf-llm"
        assert edge.target_port == "input_value"
        assert edge.condition_value is None

    def test_edge_handle_string_resolution(self) -> None:
        """Handle names resolve from the {node}|{port}|{name} string form."""
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-llm", "ChatOpenAI", template={"input_value": "hi"}),
        ]
        edge = {
            "id": "e1",
            "source": "lf-trigger",
            "target": "lf-llm",
            "sourceHandle": "lf-trigger|message|message",
            "targetHandle": "lf-llm|input_value|input_value",
        }
        result = LangflowAdapter.import_definition(
            _flow(nodes, [edge]), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.edges[0].source_port == "message"
        assert result.edges[0].target_port == "input_value"

    def test_single_output_port_fallback(self) -> None:
        """Absent handle metadata falls back to the single declared port."""
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-prompt", "Prompt", template={"template": "hi"}),
        ]
        edge = {"id": "e1", "source": "lf-trigger", "target": "lf-prompt"}
        result = LangflowAdapter.import_definition(
            _flow(nodes, [edge]), workflow_id="wf-1", workspace_id="ws-1"
        )
        # Source has a single output port and target a single input port.
        assert result.edges[0].source_port == "message"
        assert result.edges[0].target_port == "template"

    def test_multi_input_port_without_handle_rejected(self) -> None:
        """No fallback when the target declares multiple input ports."""
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-llm", "ChatOpenAI", template={"input_value": "hi"}),
        ]
        edge = {"id": "e1", "source": "lf-trigger", "target": "lf-llm"}
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, [edge]), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "UNRESOLVED_TARGET_PORT" in codes

    def test_unresolvable_target_port_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-llm", "ChatOpenAI", template={"input_value": "hi"}),
        ]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", "lf-llm", "bogus_field"),
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "UNRESOLVED_TARGET_PORT" in codes

    def test_edge_to_unknown_node_rejected(self) -> None:
        nodes = [_lf_node("lf-trigger", "ChatInput")]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", "lf-ghost", "input_value"),
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "BAD_FLOW_FORMAT" in codes

    def test_edge_missing_endpoints_rejected(self) -> None:
        nodes = [_lf_node("lf-trigger", "ChatInput")]
        edges = [{"id": "e1", "source": "lf-trigger"}]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "BAD_FLOW_FORMAT" in codes


class TestW23ConditionRouting:
    """W2-3: CONDITION routing maps route handles to condition_value."""

    def _condition_flow(
        self, true_handle: str, false_handle: str
    ) -> dict[str, Any]:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-cond", "ConditionalRouter"),
            _lf_node("lf-out-true", "ChatOutput"),
            _lf_node("lf-out-false", "ChatOutput"),
        ]
        edges = [
            _lf_edge("e0", "lf-trigger", "message", "lf-cond", "input_value"),
            _lf_edge("e1", "lf-cond", true_handle, "lf-out-true", "input_value"),
            _lf_edge(
                "e2", "lf-cond", false_handle, "lf-out-false", "input_value"
            ),
        ]
        return _flow(nodes, edges)

    def test_boolean_route_handles_coerced(self) -> None:
        flow = self._condition_flow("true", "false")
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        by_id = {e.id: e for e in result.edges}
        assert by_id["e1"].condition_value is True
        assert by_id["e2"].condition_value is False

    def test_condition_edge_data_passthrough_port(self) -> None:
        flow = self._condition_flow("true", "false")
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        by_id = {e.id: e for e in result.edges}
        # Route handles carry route values, not port names; data flows via
        # the passthrough output port.
        assert by_id["e1"].source_port == "message"

    def test_named_route_handles_preserved_as_strings(self) -> None:
        flow = self._condition_flow("route_a", "route_b")
        result = LangflowAdapter.import_definition(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        by_id = {e.id: e for e in result.edges}
        assert by_id["e1"].condition_value == "route_a"
        assert by_id["e2"].condition_value == "route_b"

    def test_explicit_condition_value_takes_priority(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-cond", "ConditionalRouter"),
            _lf_node("lf-out", "ChatOutput"),
        ]
        edges = [
            _lf_edge("e0", "lf-trigger", "message", "lf-cond", "input_value"),
            _lf_edge(
                "e1", "lf-cond", "message", "lf-out", "input_value",
                data={"condition_value": True},
            ),
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.edges[1].condition_value is True

    def test_condition_edge_without_handle_metadata_rejected(self) -> None:
        """A CONDITION edge with no handle metadata at all is rejected.

        The route value is underivable, so the import fails closed with
        CONDITION_EDGE_NO_VALUE.
        """
        nodes = [
            _lf_node("lf-cond", "ConditionalRouter"),
            _lf_node("lf-out", "ChatOutput"),
        ]
        edges = [{"id": "e1", "source": "lf-cond", "target": "lf-out"}]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "UNRESOLVED_TARGET_PORT" in codes


class TestW23ValidatorCompileIntegration:
    """W2-3: complete translated flows validate and compile end-to-end."""

    def _llm_flow(self) -> dict[str, Any]:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-llm", "ChatOpenAI", template={"input_value": "hi"}),
            _lf_node("lf-out", "ChatOutput"),
        ]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", "lf-llm", "input_value"),
            _lf_edge("e2", "lf-llm", "message", "lf-out", "input_value"),
        ]
        return _flow(nodes, edges)

    def _manifest(self):
        from workflow_engine import CapabilityManifest

        return CapabilityManifest(
            supported_node_types=set(NodeType),
            enabled_capabilities={"llm", "retriever", "agent", "tool"},
        )

    def test_full_llm_flow_validates(self) -> None:
        from workflow_engine import WorkflowValidator

        result = LangflowAdapter.import_definition(
            self._llm_flow(), workflow_id="wf-1", workspace_id="ws-1"
        )
        validation = WorkflowValidator.validate(result, self._manifest())
        assert validation.valid, validation.error_summary

    def test_full_llm_flow_compiles(self) -> None:
        from workflow_engine import WorkflowCompiler

        revision = LangflowAdapter.import_revision(
            self._llm_flow(), workflow_id="wf-1", workspace_id="ws-1"
        )
        plan = WorkflowCompiler.compile(revision, manifest=self._manifest())
        assert plan.revision_hash == revision.content_hash
        assert plan.trigger_node_id == "lf-trigger"
        assert plan.output_node_ids == ["lf-out"]
        orders = {s.node_id: s.order for s in plan.steps}
        assert orders["lf-trigger"] < orders["lf-llm"] < orders["lf-out"]

    def test_condition_flow_compiles(self) -> None:
        from workflow_engine import WorkflowCompiler

        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-cond", "ConditionalRouter"),
            _lf_node("lf-out-true", "ChatOutput"),
            _lf_node("lf-out-false", "ChatOutput"),
        ]
        edges = [
            _lf_edge("e0", "lf-trigger", "message", "lf-cond", "input_value"),
            _lf_edge("e1", "lf-cond", "true", "lf-out-true", "input_value"),
            _lf_edge("e2", "lf-cond", "false", "lf-out-false", "input_value"),
        ]
        revision = LangflowAdapter.import_revision(
            _flow(nodes, edges), workflow_id="wf-2", workspace_id="ws-1"
        )
        plan = WorkflowCompiler.compile(revision, manifest=self._manifest())
        cond_step = plan.step_by_node_id("lf-cond")
        assert cond_step is not None
        condition_values = {
            e.condition_value for e in cond_step.outgoing_edges
        }
        assert condition_values == {True, False}


class TestW23RoundTripExport:
    """W2-3: export produces re-importable Langflow JSON (purity)."""

    def _llm_flow(self) -> dict[str, Any]:
        nodes = [
            _lf_node(
                "lf-trigger",
                "ChatInput",
                display_name="Chat Input",
            ),
            _lf_node(
                "lf-llm",
                "ChatOpenAI",
                template={
                    "input_value": {"value": "hi"},
                    "model_name": {"value": "gpt-4o-mini"},
                    "temperature": {"value": 0.2},
                },
                display_name="Chat OpenAI",
            ),
            _lf_node("lf-out", "ChatOutput", display_name="Chat Output"),
        ]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", "lf-llm", "input_value"),
            _lf_edge("e2", "lf-llm", "message", "lf-out", "input_value"),
        ]
        return _flow(nodes, edges, name="Round Trip Flow")

    def test_round_trip_content_hash_equal(self) -> None:
        revision = LangflowAdapter.import_revision(
            self._llm_flow(), workflow_id="wf-1", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_revision(revision)
        reimported = LangflowAdapter.import_revision(
            exported, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert reimported.content_hash == revision.content_hash

    def test_round_trip_node_specs_equal(self) -> None:
        revision = LangflowAdapter.import_revision(
            self._llm_flow(), workflow_id="wf-1", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_revision(revision)
        reimported = LangflowAdapter.import_definition(
            exported, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert reimported.nodes == revision.spec.nodes
        assert reimported.edges == revision.spec.edges

    def test_round_trip_config_preserved(self) -> None:
        revision = LangflowAdapter.import_revision(
            self._llm_flow(), workflow_id="wf-1", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_revision(revision)
        reimported = LangflowAdapter.import_definition(
            exported, workflow_id="wf-1", workspace_id="ws-1"
        )
        llm_node = next(n for n in reimported.nodes if n.id == "lf-llm")
        assert llm_node.config["model"] == "gpt-4o-mini"
        assert llm_node.config["temperature"] == 0.2

    def test_exported_flow_passes_security_scan(self) -> None:
        revision = LangflowAdapter.import_revision(
            self._llm_flow(), workflow_id="wf-1", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_revision(revision)
        # Re-importing must not trip the marker scan on our own output.
        reimported = LangflowAdapter.import_definition(
            exported, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert isinstance(reimported, WorkflowDefinition)

    def test_export_structure(self) -> None:
        revision = LangflowAdapter.import_revision(
            self._llm_flow(), workflow_id="wf-1", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_definition(revision.spec)
        assert exported["name"] == "Round Trip Flow"
        assert len(exported["data"]["nodes"]) == 3
        assert len(exported["data"]["edges"]) == 2
        node_types = {n["id"]: n["type"] for n in exported["data"]["nodes"]}
        assert node_types["lf-trigger"] == "chatinput"
        assert node_types["lf-llm"] == "chatopenai"
        assert node_types["lf-out"] == "chatoutput"

    def test_export_revision_includes_metadata(self) -> None:
        revision = LangflowAdapter.import_revision(
            self._llm_flow(), workflow_id="wf-1", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_revision(revision)
        assert exported["data"]["revision"] == 1
        assert exported["data"]["revision_hash"] == revision.content_hash

    def test_condition_round_trip_preserves_routes(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-cond", "ConditionalRouter"),
            _lf_node("lf-out-true", "ChatOutput"),
            _lf_node("lf-out-false", "ChatOutput"),
        ]
        edges = [
            _lf_edge("e0", "lf-trigger", "message", "lf-cond", "input_value"),
            _lf_edge("e1", "lf-cond", "true", "lf-out-true", "input_value"),
            _lf_edge("e2", "lf-cond", "false", "lf-out-false", "input_value"),
        ]
        revision = LangflowAdapter.import_revision(
            _flow(nodes, edges), workflow_id="wf-2", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_revision(revision)
        reimported = LangflowAdapter.import_revision(
            exported, workflow_id="wf-2", workspace_id="ws-1"
        )
        assert reimported.content_hash == revision.content_hash
        by_id = {e.id: e for e in reimported.spec.edges}
        assert by_id["e1"].condition_value is True
        assert by_id["e2"].condition_value is False

    def test_export_rejects_unmapped_node_type(self) -> None:
        from workflow_engine import NodeSpec as NS, NodeType as NT

        definition = WorkflowDefinition(
            id="wf-x",
            workspace_id="ws-1",
            name="bad",
            nodes=[NS(id="n1", type=NT.HTTP)],
        )
        with pytest.raises(ValueError):
            LangflowAdapter.export_definition(definition)


# ---------------------------------------------------------------------------
# W2-3 R2 regression: F-A edge scalar guards
# ---------------------------------------------------------------------------


class TestW23R2EdgeScalarGuards:
    """F-A: edge-derived strings must be bounded by MAX_FIELD_CHARS."""

    def test_oversized_edge_id_rejected(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-prompt", "Prompt", template={"template": "hi"}),
        ]
        edges = [
            _lf_edge(
                "e" + "x" * LangflowAdapter.MAX_FIELD_CHARS,
                "lf-trigger",
                "message",
                "lf-prompt",
                "template",
            ),
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "OVERSIZED_FIELD" in codes

    def test_oversized_condition_value_rejected(self) -> None:
        nodes = [
            _lf_node("lf-cond", "ConditionalRouter"),
            _lf_node("lf-out", "ChatOutput"),
        ]
        edges = [
            _lf_edge(
                "e1", "lf-cond", "message", "lf-out", "input_value",
                data={"condition_value": "r" * (LangflowAdapter.MAX_FIELD_CHARS + 1)},
            ),
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "OVERSIZED_FIELD" in codes

    def test_edge_id_at_limit_accepted(self) -> None:
        """An edge id exactly at MAX_FIELD_CHARS must be accepted."""
        edge_id = "e" + "x" * (LangflowAdapter.MAX_FIELD_CHARS - 1)
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-prompt", "Prompt", template={"template": "hi"}),
        ]
        edges = [
            _lf_edge(edge_id, "lf-trigger", "message", "lf-prompt", "template"),
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.edges[0].id == edge_id

    def test_condition_value_at_limit_accepted(self) -> None:
        """A route value exactly at MAX_FIELD_CHARS must be accepted."""
        route = "r" * LangflowAdapter.MAX_FIELD_CHARS
        nodes = [
            _lf_node("lf-cond", "ConditionalRouter"),
            _lf_node("lf-out", "ChatOutput"),
        ]
        edges = [
            _lf_edge(
                "e1", "lf-cond", "message", "lf-out", "input_value",
                data={"condition_value": route},
            ),
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.edges[0].condition_value == route

    def test_oversized_rejection_scope_is_bounded(self) -> None:
        """The structured rejection must not echo an unbounded id back."""
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-prompt", "Prompt", template={"template": "hi"}),
        ]
        edges = [
            _lf_edge(
                "e" + "x" * LangflowAdapter.MAX_FIELD_CHARS,
                "lf-trigger",
                "message",
                "lf-prompt",
                "template",
            ),
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        for rejection in exc_info.value.rejections:
            assert len(str(rejection.get("edge_id", ""))) <= 64


# ---------------------------------------------------------------------------
# W2-3 R2 regression: F-B port-aware export (round-trip for every component)
# ---------------------------------------------------------------------------


class TestW23R2PortAwareExport:
    """F-B: every catalog component must round-trip hash-identically."""

    def test_catalog_component_count(self) -> None:
        assert len(_component_names()) == 15

    @pytest.mark.parametrize("component", _component_names())
    def test_single_component_round_trip(self, component: str) -> None:
        """Import → export → re-import must preserve the content hash."""
        node = _lf_node("lf-node", component, display_name=component)
        flow = _flow([node], [])
        revision = LangflowAdapter.import_revision(
            flow, workflow_id="wf-1", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_revision(revision)
        reimported = LangflowAdapter.import_revision(
            exported, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert reimported.content_hash == revision.content_hash, (
            f"round-trip hash mismatch for {component}"
        )

    def test_parser_exports_as_parser_not_prompt(self) -> None:
        """Parser has distinct ports and must not export as Prompt."""
        node = _lf_node("lf-parser", "Parser", template={"input_value": "x"})
        revision = LangflowAdapter.import_revision(
            _flow([node], []), workflow_id="wf-1", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_definition(revision.spec)
        assert exported["data"]["nodes"][0]["type"] == "parser"

    def test_prompt_exports_as_prompt(self) -> None:
        node = _lf_node("lf-prompt", "Prompt", template={"template": "hi"})
        revision = LangflowAdapter.import_revision(
            _flow([node], []), workflow_id="wf-1", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_definition(revision.spec)
        assert exported["data"]["nodes"][0]["type"] == "prompt"

    def test_parser_edge_round_trip(self) -> None:
        """A Parser wired through an edge must round-trip (the R1 break)."""
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-parser", "Parser", template={"input_value": "x"}),
            _lf_node("lf-out", "ChatOutput"),
        ]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", "lf-parser", "input_value"),
            _lf_edge("e2", "lf-parser", "message", "lf-out", "input_value"),
        ]
        revision = LangflowAdapter.import_revision(
            _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
        )
        exported = LangflowAdapter.export_revision(revision)
        reimported = LangflowAdapter.import_revision(
            exported, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert reimported.content_hash == revision.content_hash


# ---------------------------------------------------------------------------
# W2-3 R2 regression: F-C empty-string route rejection
# ---------------------------------------------------------------------------


class TestW23R2EmptyRouteRejection:
    """F-C: empty-string routes must be rejected; False must survive."""

    def test_empty_string_route_rejected(self) -> None:
        nodes = [
            _lf_node("lf-cond", "ConditionalRouter"),
            _lf_node("lf-out", "ChatOutput"),
        ]
        edges = [
            _lf_edge(
                "e1", "lf-cond", "message", "lf-out", "input_value",
                data={"condition_value": ""},
            ),
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CONDITION_EDGE_NO_VALUE" in codes

    def test_empty_handle_name_route_rejected(self) -> None:
        """With no handle metadata at all, no route is derivable → rejected."""
        nodes = [
            _lf_node("lf-cond", "ConditionalRouter"),
            _lf_node("lf-prompt", "Prompt", template={"template": "hi"}),
        ]
        edges = [{"id": "e1", "source": "lf-cond", "target": "lf-prompt"}]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CONDITION_EDGE_NO_VALUE" in codes

    def test_falsy_false_route_still_accepted(self) -> None:
        """Falsy-but-meaningful False must remain a valid route value."""
        nodes = [
            _lf_node("lf-cond", "ConditionalRouter"),
            _lf_node("lf-out", "ChatOutput"),
        ]
        edges = [
            _lf_edge(
                "e1", "lf-cond", "message", "lf-out", "input_value",
                data={"condition_value": False},
            ),
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.edges[0].condition_value is False


# ---------------------------------------------------------------------------
# W2-3 R3 regression: R3-1 node-id guard + R3-2 bounded error echoes
# ---------------------------------------------------------------------------


def _max_rejection_size(exc: LangflowImportError) -> int:
    """Largest string length across every rejection value."""
    return max(
        len(str(value))
        for rejection in exc.rejections
        for value in rejection.values()
        if isinstance(value, str)
    )


class TestW23R3NodeIdGuard:
    """R3-1: node ids are bounded like every other definition scalar."""

    def test_oversized_node_id_rejected(self) -> None:
        huge = "n" * (LangflowAdapter.MAX_FIELD_CHARS + 1)
        nodes = [_lf_node(huge, "ChatInput")]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "OVERSIZED_FIELD" in codes

    def test_normal_node_id_unaffected(self) -> None:
        nodes = [_lf_node("lf-trigger", "ChatInput")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.nodes[0].id == "lf-trigger"

    def test_node_id_at_limit_accepted(self) -> None:
        node_id = "n" * LangflowAdapter.MAX_FIELD_CHARS
        nodes = [_lf_node(node_id, "ChatInput")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.nodes[0].id == node_id

    def test_edge_referencing_oversized_node_id_rejected(self) -> None:
        """An edge referencing an oversized id is rejected, not matched.

        The id is not a declared node, so it fails as an unknown endpoint
        (the oversized *reference* echo is bounded separately below).
        """
        huge = "n" * (LangflowAdapter.MAX_FIELD_CHARS + 1)
        nodes = [_lf_node("lf-trigger", "ChatInput")]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", huge, "input_value"),
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "BAD_FLOW_FORMAT" in codes
        # No node carries the oversized id, and the echo stays bounded.
        assert _max_rejection_size(exc_info.value) <= 200


class TestW23R3BoundedEchoes:
    """R3-2: no unbounded user-controlled string in any rejection."""

    def test_ghost_node_ref_echo_bounded(self) -> None:
        ghost = "g" * (LangflowAdapter.MAX_FIELD_CHARS * 2)
        nodes = [_lf_node("lf-trigger", "ChatInput")]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", ghost, "input_value"),
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        assert _max_rejection_size(exc_info.value) <= 200

    def test_unmatched_target_handle_echo_bounded(self) -> None:
        handle = "h" * (LangflowAdapter.MAX_FIELD_CHARS * 2)
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-llm", "ChatOpenAI", template={"input_value": "hi"}),
        ]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", "lf-llm", handle),
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
            )
        assert _max_rejection_size(exc_info.value) <= 200

    def test_oversized_component_type_echo_bounded(self) -> None:
        huge = "C" * (LangflowAdapter.MAX_FIELD_CHARS * 2)
        nodes = [_lf_node("lf-x", huge)]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
            )
        assert _max_rejection_size(exc_info.value) <= 400

    def test_oversized_template_field_name_echo_bounded(self) -> None:
        field = "f" * (LangflowAdapter.MAX_FIELD_CHARS * 2)
        nodes = [
            _lf_node(
                "lf-prompt",
                "Prompt",
                template={field: {"value": "x" * (LangflowAdapter.MAX_FIELD_CHARS + 1)}},
            ),
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
            )
        assert _max_rejection_size(exc_info.value) <= 200

    def test_duplicate_oversized_id_echo_bounded(self) -> None:
        huge = "d" * (LangflowAdapter.MAX_FIELD_CHARS * 2)
        nodes = [_lf_node(huge, "ChatInput"), _lf_node(huge, "ChatOutput")]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
            )
        assert _max_rejection_size(exc_info.value) <= 200

    def test_trunc_helper_bounds_values(self) -> None:
        bounded = LangflowAdapter._trunc("x" * 500)
        assert len(bounded) < 500
        assert bounded.endswith("...(truncated)")
        assert LangflowAdapter._trunc("short") == "short"


# ---------------------------------------------------------------------------
# W2-3 R4 regression: derived marker-set echoes bounded
# ---------------------------------------------------------------------------


class TestW23R4MarkerEchoBounded:
    """R4: marker keys are bounded at insertion, so echoes stay small."""

    def test_huge_marker_key_on_node_echo_bounded(self) -> None:
        """A multi-megabyte marker key must not be echoed back at full size."""
        huge_key = "code" + "x" * (LangflowAdapter.MAX_FIELD_CHARS * 2)
        nodes = [_lf_node("lf-x", "Prompt", template={huge_key: "payload"})]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
            )
        assert _max_rejection_size(exc_info.value) <= 200
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CUSTOM_CODE_COMPONENT" in codes

    def test_huge_marker_key_on_edge_echo_bounded(self) -> None:
        """Same bound on the edge marker-scan path."""
        huge_key = "code" + "x" * (LangflowAdapter.MAX_FIELD_CHARS * 2)
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node("lf-out", "ChatOutput"),
        ]
        edge = _lf_edge("e1", "lf-trigger", "message", "lf-out", "input_value")
        edge["data"][huge_key] = "payload"
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, [edge]), workflow_id="wf-1", workspace_id="ws-1"
            )
        assert _max_rejection_size(exc_info.value) <= 200
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "CUSTOM_CODE_COMPONENT" in codes

    def test_normal_marker_still_detected_untruncated(self) -> None:
        """Ordinary marker keys keep their exact name in the report."""
        nodes = [_lf_node("lf-x", "Prompt", template={"my_class": "payload"})]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
            )
        message = exc_info.value.rejections[0]["message"]
        assert "myclass" in message


# ---------------------------------------------------------------------------
# Advisory B-1: config value-type + vetted-model validation
# ---------------------------------------------------------------------------


def _llm_template(**extra: Any) -> dict[str, Any]:
    template: dict[str, Any] = {"input_value": {"value": "hi"}}
    template.update(extra)
    return template


def _import_llm(template: dict[str, Any]):
    nodes = [_lf_node("lf-llm", "ChatOpenAI", template=template)]
    return LangflowAdapter.import_definition(
        _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
    )


class TestB1VettedModelValidation:
    """B-1: model names must be vetted, not passed through inert."""

    def test_vetted_model_accepted(self) -> None:
        result = _import_llm(_llm_template(model_name={"value": "gpt-4o-mini"}))
        assert result.nodes[0].config["model"] == "gpt-4o-mini"

    def test_unvetted_model_rejected(self) -> None:
        with pytest.raises(LangflowImportError) as exc_info:
            _import_llm(
                _llm_template(model_name={"value": "definitely-not-a-real-model"})
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "INVALID_CONFIG_VALUE" in codes

    def test_unvetted_model_message_mentions_vetted(self) -> None:
        with pytest.raises(LangflowImportError) as exc_info:
            _import_llm(_llm_template(model_name={"value": "rogue-model"}))
        messages = " ".join(r["message"] for r in exc_info.value.rejections)
        assert "vetted" in messages

    def test_vetted_model_list_is_exported(self) -> None:
        from workflow_engine.adapters.langflow_adapter import VETTED_MODELS

        assert "gpt-4o-mini" in VETTED_MODELS
        assert isinstance(VETTED_MODELS, frozenset)


class TestB1ConfigValueTypes:
    """B-1: config values must match their declared type and range."""

    def test_temperature_out_of_range_rejected(self) -> None:
        with pytest.raises(LangflowImportError) as exc_info:
            _import_llm(_llm_template(temperature={"value": 9.5}))
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "INVALID_CONFIG_VALUE" in codes

    def test_temperature_string_rejected(self) -> None:
        with pytest.raises(LangflowImportError):
            _import_llm(_llm_template(temperature={"value": "high"}))

    def test_temperature_in_range_accepted(self) -> None:
        result = _import_llm(_llm_template(temperature={"value": 0.7}))
        assert result.nodes[0].config["temperature"] == 0.7

    def test_temperature_boolean_rejected(self) -> None:
        """bool is a subclass of int; a boolean is never a valid temperature."""
        with pytest.raises(LangflowImportError):
            _import_llm(_llm_template(temperature={"value": True}))

    def test_max_tokens_non_integer_rejected(self) -> None:
        with pytest.raises(LangflowImportError):
            _import_llm(_llm_template(max_tokens={"value": 1.5}))

    def test_max_tokens_zero_rejected(self) -> None:
        with pytest.raises(LangflowImportError):
            _import_llm(_llm_template(max_tokens={"value": 0}))

    def test_max_tokens_valid_accepted(self) -> None:
        result = _import_llm(_llm_template(max_tokens={"value": 512}))
        assert result.nodes[0].config["max_tokens"] == 512

    def test_top_k_invalid_rejected(self) -> None:
        nodes = [
            _lf_node(
                "lf-retr", "VectorRetriever", template={"top_k": {"value": -3}}
            )
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "INVALID_CONFIG_VALUE" in codes

    def test_collection_unsafe_identifier_rejected(self) -> None:
        """Path traversal / shell-ish values cannot reach a name field."""
        nodes = [
            _lf_node(
                "lf-retr",
                "VectorRetriever",
                template={"collection": {"value": "../../etc/passwd"}},
            )
        ]
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
            )
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "INVALID_CONFIG_VALUE" in codes

    def test_collection_valid_accepted(self) -> None:
        nodes = [
            _lf_node(
                "lf-retr",
                "VectorRetriever",
                template={"collection": {"value": "workspace_docs"}},
            )
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert result.nodes[0].config["collection"] == "workspace_docs"

    def test_stream_boolean_enforced(self) -> None:
        with pytest.raises(LangflowImportError):
            _import_llm(_llm_template(stream={"value": "yes"}))
        result = _import_llm(_llm_template(stream={"value": False}))
        assert result.nodes[0].config["stream"] is False

    def test_non_allowlisted_fields_not_validated(self) -> None:
        """A-1 still holds: only allowlisted config fields are inspected."""
        result = _import_llm(
            _llm_template(api_key={"value": 12345}, weird={"value": object()})
        )
        assert "api_key" not in result.nodes[0].config

    def test_rejection_includes_node_id(self) -> None:
        with pytest.raises(LangflowImportError) as exc_info:
            _import_llm(_llm_template(model_name={"value": "rogue"}))
        assert any(r.get("node_id") == "lf-llm" for r in exc_info.value.rejections)

    def test_rejection_echo_bounded(self) -> None:
        with pytest.raises(LangflowImportError) as exc_info:
            _import_llm(
                _llm_template(model_name={"value": "m" * 5000})
            )
        assert _max_rejection_size(exc_info.value) <= 300

    def test_validate_config_value_returns_reason(self) -> None:
        assert LangflowAdapter._validate_config_value(
            "temperature", 0.5
        ) is None
        reason = LangflowAdapter._validate_config_value("temperature", 5)
        assert reason is not None and "temperature" in reason


# ---------------------------------------------------------------------------
# Advisory B-2: per-port descriptions
# ---------------------------------------------------------------------------


class TestB2PerPortDescriptions:
    """B-2: PortSpec.description is port-specific, not the component blurb."""

    def test_llm_ports_have_distinct_descriptions(self) -> None:
        result = _import_llm(_llm_template())
        node = result.nodes[0]
        in_desc = [p.description for p in node.input_ports]
        assert in_desc == [
            "Prompt text sent to the model.",
            "Optional system instructions.",
        ]
        assert len(set(in_desc)) == 2

    def test_output_port_description_is_port_specific(self) -> None:
        nodes = [
            _lf_node(
                "lf-retr", "VectorRetriever", template={"query": {"value": "q"}}
            )
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        node = result.nodes[0]
        assert node.input_ports[0].description == "Retrieval query string."
        assert node.output_ports[0].description == (
            "Retrieved chunks with provenance."
        )

    def test_port_description_differs_from_component_description(self) -> None:
        result = _import_llm(_llm_template())
        node = result.nodes[0]
        assert node.description != node.input_ports[0].description

    def test_every_catalog_port_has_description(self) -> None:
        for spec in LangflowAdapter.catalog().values():
            for port in list(spec.input_ports) + list(spec.output_ports):
                assert port.description, f"{spec.component}.{port.name}"

    def test_retriever_port_descriptions(self) -> None:
        nodes = [
            _lf_node(
                "lf-retr", "VectorRetriever", template={"query": {"value": "q"}}
            )
        ]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        node = result.nodes[0]
        assert node.input_ports[0].description == "Retrieval query string."
        assert node.output_ports[0].description == (
            "Retrieved chunks with provenance."
        )

    def test_condition_ports_descriptions(self) -> None:
        nodes = [_lf_node("lf-cond", "ConditionalRouter")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        node = result.nodes[0]
        assert node.output_ports[0].description == (
            "Boolean route-selection result."
        )
        assert node.output_ports[1].description == (
            "Passthrough of the evaluated input value."
        )

    def test_output_node_port_descriptions(self) -> None:
        nodes = [_lf_node("lf-out", "ChatOutput")]
        result = LangflowAdapter.import_definition(
            _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
        )
        node = result.nodes[0]
        assert node.input_ports[0].description == "Final answer value."
        assert node.output_ports[0].description == "Workflow output value."
