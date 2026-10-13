"""T-V4-007 — port type-compatibility hardening: matrix, generics, nullability.

The compatibility rules are STATED in ``workflow_engine.validator`` (an
explicit matrix), not emergent from an any-swallows-everything comparison:

* the matrix — identity pairs plus the declared ``any`` wildcard, nothing
  else; any pair absent refuses;
* generics — ``list<A>`` into ``list<B>`` compares ELEMENT types recursively
  (elements, not container — the B-R1-3 discipline applied to types);
* nullability — a trailing ``?`` on the port type marks the port nullable;
  a nullable source may only feed a nullable target.  Per-port, never a
  global escape hatch.

Both metrics are proven: mismatched wires accepted: 0 (off-matrix battery,
nested generics, nullable abuse — all refused typed) and valid wires
rejected: 0 (matrix edges, generics, legacy flat flows).
"""

from __future__ import annotations

import pytest
from workflow_engine import (
    CapabilityManifest,
    NodeSpec,
    NodeType,
    PortSpec,
    ValidationError,
    WorkflowDefinition,
    WorkflowEdgeSpec,
    WorkflowValidator,
)
from workflow_engine.interfaces import _is_supported_port_type
from workflow_engine.validator import (
    _BASE_PORT_TYPES,
    _PORT_TYPE_MATRIX,
    _mismatch_reason,
    _types_compatible,
)

BASES = sorted(_BASE_PORT_TYPES)


@pytest.fixture
def manifest() -> CapabilityManifest:
    return CapabilityManifest(
        supported_node_types=set(NodeType),
        enabled_capabilities={"llm", "retriever", "agent", "tool", "http"},
    )


def _node(
    node_id: str,
    out_type: str = "any",
    in_type: str = "any",
    *,
    node_type: NodeType = NodeType.TRANSFORM,
    out_name: str = "output",
    in_name: str = "input",
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        input_ports=[PortSpec(name=in_name, type=in_type)],
        output_ports=[PortSpec(name=out_name, type=out_type)],
    )


def _edge(edge_id: str, source: str, target: str) -> WorkflowEdgeSpec:
    return WorkflowEdgeSpec(
        id=edge_id,
        source_node_id=source,
        source_port="output",
        target_node_id=target,
        target_port="input",
    )


def _validate_pair(source_type: str, target_type: str, manifest: CapabilityManifest):
    """Validate a minimal two-node definition wired source → target."""
    definition = WorkflowDefinition(
        id="wf-types",
        workspace_id="ws-1",
        name="types",
        nodes=[_node("src", out_type=source_type), _node("dst", in_type=target_type)],
        edges=[_edge("e1", "src", "dst")],
    )
    return WorkflowValidator.validate(definition, manifest)


def _type_errors(result) -> list[ValidationError]:
    return [e for e in result.errors if e.code == "TYPE_MISMATCH"]


# ---------------------------------------------------------------------------
# 1. The matrix itself — stated, and the behavior matches it exactly
# ---------------------------------------------------------------------------


class TestMatrixIsTheBehavior:
    def test_matrix_contains_exactly_identity_and_any_pairs(self) -> None:
        """The stated matrix: identity pairs + the declared any wildcard."""
        expected = (
            {(b, b) for b in BASES}
            | {("any", b) for b in BASES}
            | {(b, "any") for b in BASES}
        )
        assert set(_PORT_TYPE_MATRIX) == expected

    def test_compatibility_matches_matrix_for_every_base_pair(self) -> None:
        """No emergent rule: for all 36 base pairs, behavior == matrix."""
        for source in BASES:
            for target in BASES:
                expected = (source, target) in _PORT_TYPE_MATRIX
                assert _types_compatible(source, target) is expected, (
                    f"{source} -> {target} disagrees with the matrix"
                )

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            ("string", "number"),
            ("number", "string"),
            ("string", "boolean"),
            ("boolean", "number"),
            ("string", "dict"),
            ("dict", "string"),
            ("dict", "list"),
            ("list", "dict"),
            ("number", "dict"),
            ("list", "boolean"),
        ],
    )
    def test_off_matrix_pairs_refuse(self, source: str, target: str) -> None:
        assert _types_compatible(source, target) is False


# ---------------------------------------------------------------------------
# 2. Off-matrix battery through the validator — typed, names both ports/types
# ---------------------------------------------------------------------------


class TestOffMatrixBatteryRefusesTyped:
    @pytest.mark.parametrize(
        ("source", "target"),
        [
            ("string", "number"),
            ("number", "string"),
            ("string", "boolean"),
            ("boolean", "number"),
            ("string", "dict"),
            ("dict", "string"),
            ("dict", "list"),
            ("list", "dict"),
            ("number", "dict"),
            ("list", "boolean"),
            ("string", "list"),
            ("list", "string"),
        ],
    )
    def test_mismatched_wire_refused_with_typed_error(
        self, source: str, target: str, manifest: CapabilityManifest
    ) -> None:
        result = _validate_pair(source, target, manifest)
        errors = _type_errors(result)
        assert len(errors) == 1, f"{source}->{target}: expected exactly one TYPE_MISMATCH"
        message = errors[0].message
        # Names the edge, BOTH ports, and BOTH types.
        assert "e1" in message
        assert "output" in message and "input" in message
        assert source in message and target in message
        # Reason class stated, payload bounded.
        assert "compatibility matrix" in message
        assert len(message) <= 300

    def test_matrix_edge_valid_pairs_accepted(self, manifest: CapabilityManifest) -> None:
        """Valid wires rejected: 0 — identity pairs and the any wildcard pass."""
        for source, target in [
            ("string", "string"),
            ("number", "number"),
            ("boolean", "boolean"),
            ("dict", "dict"),
            ("list", "list"),
            ("any", "any"),
            ("any", "string"),
            ("string", "any"),
            ("any", "list"),
        ]:
            result = _validate_pair(source, target, manifest)
            assert _type_errors(result) == [], f"{source}->{target} over-rejected"


# ---------------------------------------------------------------------------
# 3. Generics — element types checked, not the container
# ---------------------------------------------------------------------------


class TestGenericElementTypes:
    def test_identical_parameterized_lists_accepted(self, manifest: CapabilityManifest) -> None:
        result = _validate_pair("list<string>", "list<string>", manifest)
        assert _type_errors(result) == []

    def test_nested_identical_parameterized_lists_accepted(
        self, manifest: CapabilityManifest
    ) -> None:
        result = _validate_pair("list<list<number>>", "list<list<number>>", manifest)
        assert _type_errors(result) == []

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            ("list<string>", "list<number>"),
            ("list<number>", "list<string>"),
            ("list<string>", "list<boolean>"),
            ("list<list<number>>", "list<list<string>>"),
            ("list<list<string>>", "list<list<boolean>>"),
        ],
    )
    def test_element_mismatch_refused(
        self, source: str, target: str, manifest: CapabilityManifest
    ) -> None:
        """Elements, not container: the container matches, the elements do not."""
        result = _validate_pair(source, target, manifest)
        errors = _type_errors(result)
        assert len(errors) == 1
        assert "list element types differ" in errors[0].message
        assert source in errors[0].message and target in errors[0].message

    def test_container_mismatch_refused(self, manifest: CapabilityManifest) -> None:
        """A parameterized list is still a list: list<string> → dict refuses."""
        result = _validate_pair("list<string>", "dict", manifest)
        assert len(_type_errors(result)) == 1

    def test_bare_list_is_any_element_both_directions(
        self, manifest: CapabilityManifest
    ) -> None:
        """Bare ``list`` = ``list<any>`` (explicitly untyped element)."""
        assert _type_errors(_validate_pair("list", "list<string>", manifest)) == []
        assert _type_errors(_validate_pair("list<string>", "list", manifest)) == []

    def test_any_element_wildcard(self, manifest: CapabilityManifest) -> None:
        assert _type_errors(_validate_pair("list<any>", "list<string>", manifest)) == []
        assert _type_errors(_validate_pair("list<string>", "list<any>", manifest)) == []


# ---------------------------------------------------------------------------
# 4. Nullability — explicit per port, never a global escape hatch
# ---------------------------------------------------------------------------


class TestNullability:
    def test_non_null_into_nullable_accepted(self, manifest: CapabilityManifest) -> None:
        assert _type_errors(_validate_pair("string", "string?", manifest)) == []

    def test_nullable_into_nullable_accepted(self, manifest: CapabilityManifest) -> None:
        assert _type_errors(_validate_pair("string?", "string?", manifest)) == []

    def test_nullable_into_non_nullable_refused(self, manifest: CapabilityManifest) -> None:
        """None-handling per port: a possibly-None source never enters a
        non-nullable target — and this is NOT a global escape hatch."""
        result = _validate_pair("string?", "string", manifest)
        errors = _type_errors(result)
        assert len(errors) == 1
        assert "nullable source into non-nullable target" in errors[0].message

    @pytest.mark.parametrize("base", sorted(_BASE_PORT_TYPES - {"any"}))
    def test_nullable_abuse_battery_refuses(self, base: str, manifest: CapabilityManifest) -> None:
        """Every nullable source into a non-nullable target of the same base
        refuses; into a nullable target of the same base it is accepted."""
        assert len(_type_errors(_validate_pair(f"{base}?", base, manifest))) == 1
        assert _type_errors(_validate_pair(f"{base}?", f"{base}?", manifest)) == []

    def test_nullable_does_not_rescue_base_mismatch(self, manifest: CapabilityManifest) -> None:
        """``string?`` → ``number?`` is still an off-matrix pair."""
        assert len(_type_errors(_validate_pair("string?", "number?", manifest))) == 1

    def test_nullable_inside_generics(self, manifest: CapabilityManifest) -> None:
        """Element nullability is checked by the same recursion."""
        assert _type_errors(_validate_pair("list<string>", "list<string?>", manifest)) == []
        assert len(_type_errors(_validate_pair("list<string?>", "list<string>", manifest))) == 1

    def test_nullable_container(self, manifest: CapabilityManifest) -> None:
        assert _type_errors(_validate_pair("list<string>", "list<string>?", manifest)) == []
        assert len(_type_errors(_validate_pair("list<string>?", "list<string>", manifest))) == 1


# ---------------------------------------------------------------------------
# 5. Syntax acceptance at the contract boundary
# ---------------------------------------------------------------------------


class TestPortTypeSyntax:
    @pytest.mark.parametrize(
        "port_type",
        [
            "string", "number", "boolean", "dict", "list", "any",
            "string?", "list?", "any?",
            "list<string>", "list<any>", "list<list<string>>",
            "list<list<list<number>>>?", "dict?", "number?",
        ],
    )
    def test_syntax_accepted(self, port_type: str) -> None:
        assert _is_supported_port_type(port_type) is True
        PortSpec(name="p", type=port_type)  # constructible

    @pytest.mark.parametrize(
        "port_type",
        [
            "", "list<>", "listlist", "string??", "list<string", "list<string>>",
            "dict<string>", "any<string>", "list<>?", "number|",
            "list<list<list<list<list<list<list<list<list<string>>>>>>>>>>",  # depth 10 > cap
        ],
    )
    def test_syntax_rejected(self, port_type: str) -> None:
        assert _is_supported_port_type(port_type) is False
        with pytest.raises(ValueError):
            PortSpec(name="p", type=port_type)

    def test_dangling_ports_still_skip_the_type_check(self, manifest: CapabilityManifest) -> None:
        """Unknown ports are reported by the dangling check, not here."""
        definition = WorkflowDefinition(
            id="wf-dangling",
            workspace_id="ws-1",
            name="dangling",
            nodes=[_node("src", out_type="string"), _node("dst", in_type="string")],
            edges=[
                WorkflowEdgeSpec(
                    id="e1",
                    source_node_id="src",
                    source_port="nope",  # unknown source port
                    target_node_id="dst",
                    target_port="input",
                )
            ],
        )
        result = WorkflowValidator.validate(definition, manifest)
        assert _type_errors(result) == []
        assert "DANGLING_SOURCE_PORT" in {e.code for e in result.errors}


# ---------------------------------------------------------------------------
# 6. Legacy flat flows — no over-rejection
# ---------------------------------------------------------------------------


class TestLegacyFlatFlowUnaffected:
    def test_flat_any_typed_flow_validates(self, manifest: CapabilityManifest) -> None:
        """The pre-T-V4-007 shapes (flat types, any ports) behave as before."""
        definition = WorkflowDefinition(
            id="wf-flat",
            workspace_id="ws-1",
            name="flat",
            nodes=[
                _node("trigger", out_type="dict", node_type=NodeType.TRIGGER),
                _node("transform", out_type="string", in_type="any"),
                _node("out", in_type="any", node_type=NodeType.OUTPUT),
            ],
            edges=[
                _edge("e1", "trigger", "transform"),
                _edge("e2", "transform", "out"),
            ],
        )
        result = WorkflowValidator.validate(definition, manifest)
        assert _type_errors(result) == []
        assert result.valid, result.error_summary

    def test_mismatch_reason_helper_bounded(self) -> None:
        pairs = [("string?", "string"), ("list<string>", "list<number>"), ("dict", "list")]
        for source, target in pairs:
            reason = _mismatch_reason(source, target)
            assert reason
            assert len(reason) <= 120
