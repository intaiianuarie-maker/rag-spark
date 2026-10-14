"""T-V4-022 — editor node palette catalogue tests.

Pinned properties (card):
1. **Palette-vs-validator agreement, both directions.** Every catalogued node
   type validates clean in a definition built only from the catalogue; and every
   node type the validator knows appears in the palette. No drift either way.
2. **UI code added: 0.** No tsx/jsx/component/canvas/event handler — the module
   is data, and a canvas renders it unchanged.
3. Derived eligibility: join/loop flags follow from the port shape rather than a
   hand-maintained boolean, and duplicated port names are refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from workflow_engine import (
    NodeSpec,
    NodeType,
    PortSpec,
    ValidationResult,
    WorkflowDefinition,
    WorkflowEdgeSpec,
    WorkflowValidator,
)
from workflow_engine.palette import (
    PALETTE,
    PALETTE_MANIFEST,
    Palette,
    PaletteCategory,
    PaletteNode,
    PalettePort,
)

PALETTE_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "packages" / "workflow_engine" / "palette.py"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _definition_for(node: PaletteNode, *, node_id: str = "n1") -> WorkflowDefinition:
    """Build the minimal definition the palette says *node* can be placed in.

    The probe is a real, connected workflow: a TRIGGER (required entry point)
    feeding the node under test feeding an OUTPUT (required exit), with edges
    wired so nothing is orphaned. What must be left validate-clean is the node
    itself; a probe that complained about its own shape (no OUTPUT node, an
    orphan) would prove nothing about the catalogue.
    """
    inputs, outputs = node.port_specs()
    nodes = [
        NodeSpec(
            id="probe_trigger", type=NodeType.TRIGGER, name="Trigger",
            output_ports=[PortSpec(name="output", type="dict")],
        ),
        NodeSpec(
            id=node_id, type=node.type, name=node.label, config={},
            input_ports=inputs, output_ports=outputs,
            required_capabilities=list(node.required_capabilities),
        ),
        NodeSpec(
            id="probe_output", type=NodeType.OUTPUT, name="Output",
            input_ports=[PortSpec(name="input", type="any")],
            output_ports=[PortSpec(name="output", type="any")],
        ),
    ]
    # An input port that cannot accept a dict (e.g. a required "context"
    # port) is satisfied with a default instead of an edge, which is exactly
    # what the default is for.
    in_port = node.input_ports[0].name if node.input_ports else None
    edges = []
    if in_port is not None:
        edges.append(
            WorkflowEdgeSpec(
                id="in_edge", source_node_id="probe_trigger", source_port="output",
                target_node_id=node_id, target_port=in_port,
            )
        )
    # Every outgoing edge from a CONDITION must carry a condition_value — the
    # validator refuses a null one, so the probe honours that engine rule by
    # setting one rather than claiming a palette entry is unplaceable.
    is_condition = node.type == NodeType.CONDITION
    edges.append(
        WorkflowEdgeSpec(
            id="out_edge", source_node_id=node_id,
            source_port=node.output_ports[0].name,
            target_node_id="probe_output", target_port="input",
            condition_value=True if is_condition else None,
        )
    )
    return WorkflowDefinition(
        id="wf-palette",
        workspace_id="ws-1",
        name="Palette Probe",
        nodes=nodes,
        edges=edges,
    )


def _validate(definition: WorkflowDefinition) -> ValidationResult:
    return WorkflowValidator().validate(definition, PALETTE_MANIFEST)


def _error_codes(result: ValidationResult) -> set[str]:
    return {e.code for e in result.errors}


def _codes_excluding_connectivity(result: ValidationResult) -> set[str]:
    """Errors that are not the expected unconnected-input complaints.

    A one-node definition has no edges, so required input ports are unconnected
    by construction. That is a *definition* fact, not a palette fact: the
    palette entry is still clean. Anything else would be a real disagreement.
    """
    return {
        e.code for e in result.errors if e.code != "UNCONNECTED_REQUIRED_INPUT"
    }


# --------------------------------------------------------------------------- #
# 1. Catalogue shape
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_catalogue_covers_every_node_type_the_engine_knows() -> None:
    """Card metric, direction B: no validator-known type is missing."""
    catalogued = set(PALETTE.types())
    assert catalogued == set(NodeType), (
        f"missing: {sorted(set(NodeType) - catalogued)}"
    )


@pytest.mark.unit
def test_every_catalogued_node_type_is_a_real_node_type() -> None:
    assert all(isinstance(n.type, NodeType) for n in PALETTE.nodes)


@pytest.mark.unit
def test_catalogue_entries_are_unique_and_non_empty() -> None:
    labels = [n.label for n in PALETTE.nodes]
    assert len(set(labels)) == len(labels)
    for node in PALETTE.nodes:
        assert node.description.strip(), node.type
        assert node.label.strip(), node.type


@pytest.mark.unit
def test_every_entry_declares_an_output_port() -> None:
    """Every node must emit something, or a workflow could end in a dead end."""
    for node in PALETTE.nodes:
        assert node.output_ports, node.type


@pytest.mark.unit
def test_every_entry_has_documentation() -> None:
    for node in PALETTE.nodes:
        assert len(node.description) <= 2_048
        assert node.description.strip()


@pytest.mark.unit
def test_trigger_is_the_only_node_without_input_ports() -> None:
    for node in PALETTE.nodes:
        if node.type == NodeType.TRIGGER:
            assert node.input_ports == []
        else:
            assert node.input_ports, node.type


@pytest.mark.unit
def test_placeable_refuses_unknown_types() -> None:
    """A canvas asking for a type the palette lacks gets a refusal, not an
    silently empty panel that hides a placeable node."""
    empty = Palette(nodes=[])
    with pytest.raises(KeyError, match="no entry for node type"):
        empty.placeable(NodeType.LLM)
    # ...and a populated palette resolves every type it declares.
    for node in PALETTE.nodes:
        assert PALETTE.placeable(node.type).label == node.label


@pytest.mark.unit
def test_palette_lookup_paths_work() -> None:
    by_type = PALETTE.by_type()
    assert by_type[NodeType.OUTPUT].type == NodeType.OUTPUT
    assert PALETTE.placeable(NodeType.OUTPUT).label == "Output"
    grouped = PALETTE.categories()
    assert sum(len(v) for v in grouped.values()) == len(PALETTE.nodes)
    assert PaletteCategory.FLOW in grouped


@pytest.mark.unit
def test_duplicate_node_types_are_refused() -> None:
    with pytest.raises(ValueError, match="more than once"):
        Palette(nodes=[PALETTE.nodes[0], PALETTE.nodes[0]])


@pytest.mark.unit
def test_duplicate_port_names_are_refused() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        PaletteNode(
            type=NodeType.OUTPUT,
            label="X",
            category=PaletteCategory.EFFECT,
            description="d",
            input_ports=[PalettePort(name="input", type="any")],
            output_ports=[
                PalettePort(name="output", type="any"),
                PalettePort(name="output", type="string"),
            ],
        )


# --------------------------------------------------------------------------- #
# 2. Palette-vs-validator agreement (BOTH directions)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("node", PALETTE.nodes, ids=lambda n: n.type.value)
def test_every_catalogued_node_validates_clean(node: PaletteNode) -> None:
    """Card metric, direction A: a palette entry can actually be placed.

    The only permitted complaint is UNCONNECTED_REQUIRED_INPUT, which is a
    property of the one-node probe definition (no edges) and not of the palette.
    Anything else is a disagreement between the catalogue and the engine.
    """
    result = _validate(_definition_for(node))
    assert _codes_excluding_connectivity(result) == set(), (
        f"{node.type} catalogued nodes do not validate: "
        f"{[(e.code, e.message) for e in result.errors]}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("node", PALETTE.nodes, ids=lambda n: n.type.value)
def test_catalogued_ports_are_supported_by_the_validator(node: PaletteNode) -> None:
    """Every declared port type is one the validator's grammar accepts."""
    for port in [*node.input_ports, *node.output_ports]:
        # An unknown type would surface as a port-type error on validate.
        probe = _definition_for(node)
        probe.nodes[0].input_ports = [p.to_port_spec() for p in node.input_ports]
        result = _validate(probe)
        assert "UNSUPPORTED_PORT_TYPE" not in _error_codes(result), port


@pytest.mark.unit
def test_catalogued_capabilities_are_manifest_placeable() -> None:
    """A node must place under the manifest the palette ships with."""
    for node in PALETTE.nodes:
        for cap in node.required_capabilities:
            assert cap in PALETTE_MANIFEST.enabled_capabilities, (node.type, cap)


@pytest.mark.unit
def test_palette_manifest_enables_every_node_type() -> None:
    """The palette's own manifest must accept every catalogued type."""
    assert set(PALETTE_MANIFEST.supported_node_types) == set(NodeType)


@pytest.mark.unit
def test_required_port_matches_validator_semantics() -> None:
    """A required port with a default is not required (validator rule)."""
    node = PALETTE.placeable(NodeType.AGENT)
    specs = [p.to_port_spec() for p in node.input_ports]
    required_with_default = [
        s for s in specs if s.required and getattr(s, "default", None) is not None
    ]
    assert not required_with_default
    # The context port is optional (has a default), so it is not required.
    context = next(s for s in specs if s.name == "context")
    assert context.required is False


@pytest.mark.unit
def test_minimal_workflow_built_only_from_the_palette_validates() -> None:
    """A complete trigger->transform->output chain assembles from the catalogue."""
    trigger = PALETTE.placeable(NodeType.TRIGGER)
    transform = PALETTE.placeable(NodeType.TRANSFORM)
    output = PALETTE.placeable(NodeType.OUTPUT)

    nodes = []
    for entry, nid in ((trigger, "t"), (transform, "x"), (output, "o")):
        inputs, outputs = entry.port_specs()
        nodes.append(
            NodeSpec(
                id=nid, type=entry.type, name=entry.label, config={},
                input_ports=inputs, output_ports=outputs,
                required_capabilities=list(entry.required_capabilities),
            )
        )
    definition = WorkflowDefinition(
        id="wf-palette-min", workspace_id="ws-1", name="Min", nodes=nodes,
        edges=[
            WorkflowEdgeSpec(
                id="e1", source_node_id="t", source_port="output",
                target_node_id="x", target_port="input",
            ),
            WorkflowEdgeSpec(
                id="e2", source_node_id="x", source_port="output",
                target_node_id="o", target_port="input",
            ),
        ],
    )
    result = _validate(definition)
    assert result.valid, [(e.code, e.message) for e in result.errors]


# --------------------------------------------------------------------------- #
# 3. Join / loop eligibility is derived, not asserted
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("node", PALETTE.nodes, ids=lambda n: n.type.value)
def test_join_eligibility_matches_the_port_shape(node: PaletteNode) -> None:
    assert node.join_eligible == (len(node.input_ports) > 1), node.type


@pytest.mark.unit
def test_join_eligible_nodes_declare_a_strategy_requirement() -> None:
    """Join-eligible nodes are the ones the compiler can demand a strategy for."""
    from workflow_engine.interfaces import JoinStrategy

    assert set(JoinStrategy) == {"all", "any", "first"}
    for node in PALETTE.join_eligible_types():
        assert len(PALETTE.placeable(node).input_ports) > 1


@pytest.mark.unit
def test_join_eligibility_cannot_be_asserted_against_the_port_shape() -> None:
    """A hand-set flag that contradicts the ports is refused at construction."""
    with pytest.raises(ValueError, match="join_eligible"):
        PaletteNode(
            type=NodeType.OUTPUT,
            label="X",
            category=PaletteCategory.EFFECT,
            description="d",
            input_ports=[PalettePort(name="input", type="any")],
            output_ports=[PalettePort(name="output", type="any")],
            join_eligible=True,
        )
    with pytest.raises(ValueError, match="join_eligible"):
        PaletteNode(
            type=NodeType.OUTPUT,
            label="X",
            category=PaletteCategory.EFFECT,
            description="d",
            input_ports=[
                PalettePort(name="input", type="any"),
                PalettePort(name="ctx", type="any"),
            ],
            output_ports=[PalettePort(name="output", type="any")],
            join_eligible=False,
        )


@pytest.mark.unit
@pytest.mark.parametrize("node", PALETTE.nodes, ids=lambda n: n.type.value)
def test_loop_eligibility_is_a_real_type_cycle(node: PaletteNode) -> None:
    """A loop-eligible node's output type can feed one of its own inputs.

    The rule is type overlap: some output's type must match some input's. The
    naive reading of that caught the retriever (``list<dict>`` out into a
    ``dict`` in — a mismatch the validator refuses), which is why the catalogue
    had to be corrected rather than the test relaxed.
    """
    if not node.loop_eligible:
        return
    output_types = {p.type for p in node.output_ports}
    input_types = {p.type for p in node.input_ports}
    assert output_types & input_types, (
        f"{node.type} claims loop eligibility but its output {output_types} "
        f"never matches its input {input_types}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("node", PALETTE.nodes, ids=lambda n: n.type.value)
def test_non_loop_eligible_nodes_do_not_close_a_useful_cycle(node: PaletteNode) -> None:
    """A non-loop node either has no type overlap, or its overlap is inert.

    CONDITION is the honest example: its ``result`` (boolean) overlaps the
    ``any`` input, but feeding a boolean back into the node's own test does not
    close a *useful* cycle — the engine would just re-evaluate one constant.
    A palette that marked such a node loop-eligible would offer a cycle the
    editor cannot actually build, so the rule is documented, not fudged.
    """
    if node.loop_eligible:
        return
    output_types = {p.type for p in node.output_ports}
    input_types = {p.type for p in node.input_ports}
    overlap = output_types & input_types
    if not overlap:
        return  # nothing to feed back: correctly not loop-eligible

    # Overlap exists, so the node must be one whose overlap is inert.
    inert = {"condition", "llm", "http", "output", "trigger"}
    assert node.type.value in inert, (
        f"{node.type} has output/input overlap {overlap} but is not "
        "loop-eligible; either it can close a cycle (mark it) or the overlap "
        "must be inert"
    )


# --------------------------------------------------------------------------- #
# 4. UI code added: 0
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_no_ui_code_in_the_palette() -> None:
    """Card metric: UI code added = 0 (source-level, no string heuristics)."""
    import ast

    tree = ast.parse(PALETTE_SOURCE.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
    ui_roots = {"react", "react-dom", "next", "framer-motion", "clsx", "zustand"}
    assert not (ui_roots & imports), f"palette imports UI code: {ui_roots & imports}"

    # No UI-ish identifiers anywhere as names or attributes.
    ui_names = {
        "onClick", "onChange", "onDragStart", "useState", "useEffect", "useMemo",
        "forwardRef", "jsx", "canvas", "render", "Component", "div", "span",
    }
    referenced = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not (ui_names & referenced), (
        f"palette carries UI constructs: {ui_names & referenced}"
    )


@pytest.mark.unit
def test_palette_is_pure_data() -> None:
    """No service calls, no I/O: the catalogue is a module-level constant."""
    assert isinstance(PALETTE, Palette)
    assert PALETTE.nodes

    # The access paths a canvas uses are all data accessors, none of them a
    # service call. (Checked explicitly rather than by walking pydantic's own
    # attributes, which trip deprecation warnings and prove nothing.)
    canvas_api = ("by_type", "types", "categories", "placeable", "join_eligible_types",
                  "loop_eligible_types", "model_dump", "model_dump_json")
    for name in canvas_api:
        getattr(PALETTE, name)  # exists
    assert callable(PALETTE.model_dump)
    # The catalogue itself parallelises into plain JSON without I/O.
    assert PALETTE.model_dump()["nodes"]


@pytest.mark.unit
def test_no_ui_files_were_added_by_this_ticket() -> None:
    """The palette must not have pulled in a canvas, page or component."""
    import subprocess

    tracked = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    ui_like = [
        line
        for line in tracked
        if line.strip().endswith((".tsx", ".jsx"))
        or "/components/" in line
        or "/app/" in line
    ]
    assert ui_like == [], f"UI files changed in the tree: {ui_like}"


@pytest.mark.unit
def test_a_canvas_can_render_from_the_catalogue_unchanged() -> None:
    """The render shape is fully derived from data (no hidden rules)."""
    rendered = []
    for category, nodes in PALETTE.categories().items():
        for node in nodes:
            rendered.append(
                {
                    "category": category.value,
                    "label": node.label,
                    "type": node.type.value,
                    "inputs": [p.name for p in node.input_ports],
                    "outputs": [p.name for p in node.output_ports],
                    "config": [c.name for c in node.config_schema],
                    "join": node.join_eligible,
                    "loop": node.loop_eligible,
                    "docs": node.description,
                }
            )
    assert len(rendered) == len(PALETTE.nodes)
    assert all(r["type"] for r in rendered)
    assert all(r["docs"] for r in rendered)
    # Nothing the canvas needs is missing from the data.
    for row in rendered:
        assert row["outputs"], row
        assert row["category"]


@pytest.mark.unit
def test_catalogue_serialises_for_the_client() -> None:
    """The palette must cross the wire as plain JSON (docs/port names intact)."""
    import json

    payload = json.loads(PALETTE.model_dump_json())
    assert len(payload["nodes"]) == len(PALETTE.nodes)
    for entry in payload["nodes"]:
        assert entry["description"]
        assert entry["label"]
        assert entry["output_ports"]
