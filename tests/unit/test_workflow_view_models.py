"""T-V4-023 — validator-feedback + run-timeline view model tests.

Pinned properties (card):
1. **Validation errors unmapped: 0** — every code the real validator emits maps
   to an annotation carrying node id + message (and port when the error names
   one); an unknown code is REFUSED, never dropped.
2. **Run information dropped: 0** — every node run, the terminal state, every
   skip reason, the failing node and the timing all appear in the timeline.
3. **UI code added: 0** — the same triple proof as T-V4-022.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest
from workflow_engine import (
    FakeNodeExecutor,
    NodeRun,
    NodeRunStatus,
    NodeSpec,
    NodeType,
    PortSpec,
    ValidationError,
    ValidationResult,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
)
from workflow_engine.view_models import (
    AnnotationSeverity,
    EditorAnnotation,
    TimelineEntry,
    TimelineEntryState,
    TimelineTerminalState,
    ValidationFeedback,
    ViewModelError,
    build_annotation,
    build_annotations,
    build_timeline,
    build_validation_feedback,
)

VIEW_MODELS = (
    Path(__file__).resolve().parents[2] / "packages" / "workflow_engine" / "view_models.py"
)
VALIDATOR = Path(__file__).resolve().parents[2] / "packages" / "workflow_engine" / "validator.py"


def _run(
    *,
    status: WorkflowRunStatus = WorkflowRunStatus.COMPLETED,
    node_runs: dict | None = None,
    failed_node_id: str | None = None,
    error: str | None = None,
    final_output: object = None,
    created_at: str = "2026-10-14T10:00:00+00:00",
    finished_at: str | None = "2026-10-14T10:00:02+00:00",
) -> WorkflowRun:
    runs = {}
    for node_id, spec in (node_runs or {}).items():
        runs[node_id] = NodeRun(**spec)
    return WorkflowRun(
        id="run-1",
        workspace_id="ws-1",
        workflow_id="wf-1",
        status=status,
        node_runs=runs,
        final_output=final_output,
        created_at=created_at,
        finished_at=finished_at,
        error=error,
        failed_node_id=failed_node_id,
    )


def _completed(node_id: str, output: object = "out", **kw) -> dict:
    return {
        node_id: {
            "node_id": node_id,
            "status": NodeRunStatus.COMPLETED,
            "output_data": output,
            "latency_ms": kw.get("latency_ms", 12.5),
            "attempts": kw.get("attempts", 1),
        }
    }


# --------------------------------------------------------------------------- #
# 1. Validation errors unmapped: 0
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_mapping_table_covers_every_code_the_validator_emits() -> None:
    """Card metric, direction A: no real validator code is missing.

    The codes are read out of the *validator source*, so a new one added there
    without a table entry fails this test.
    """
    from workflow_engine.view_models import _ANNOTATION_TABLE

    emitted = set()
    tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            pass
    # Structural pull: every keyword argument `code="..."` in the validator.
    source = VALIDATOR.read_text(encoding="utf-8")
    for segment in source.split("code=")[1:]:
        literal = segment.split('"')[1] if '"' in segment else segment.split("'")[1]
        emitted.add(literal)

    assert emitted == set(_ANNOTATION_TABLE), (
        f"missing from the mapping table: {sorted(emitted - set(_ANNOTATION_TABLE))}; "
        f"stale in it: {sorted(set(_ANNOTATION_TABLE) - emitted)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "code",
    [
        "EMPTY_WORKFLOW",
        "NO_TRIGGER",
        "NO_OUTPUT_NODE",
        "DUPLICATE_NODE_ID",
        "DUPLICATE_EDGE_ID",
        "DUPLICATE_PORT_NAME",
        "EMPTY_PORT_NAME",
        "CYCLE_DETECTED",
        "ORPHANED_NODE",
        "DANGLING_SOURCE",
        "DANGLING_TARGET",
        "DANGLING_SOURCE_PORT",
        "DANGLING_TARGET_PORT",
        "UNCONNECTED_REQUIRED_INPUT",
        "TYPE_MISMATCH",
        "CONDITION_EDGE_NO_VALUE",
        "UNSUPPORTED_NODE_TYPE",
        "UNSUPPORTED_CAPABILITY",
        "TOO_MANY_NODES",
        "TOO_MANY_EDGES",
        "NODE_FAN_OUT_TOO_HIGH",
        "NODE_CONFIG_TOO_LARGE",
        "GRAPH_CONFIG_TOO_LARGE",
    ],
)
def test_every_validator_code_maps_to_an_annotation(code: str) -> None:
    """Card metric: node id + message survive, and nothing is dropped."""
    error = ValidationError(code=code, message=f"Node 'n1' has a {code} problem", node_id="n1")
    annotation = build_annotation(error)
    assert annotation.code == code
    assert annotation.node_id == "n1"
    assert code in annotation.message  # the message survived verbatim
    assert annotation.severity in set(AnnotationSeverity)
    assert annotation.blocking is True


@pytest.mark.unit
def test_an_unknown_code_is_refused_never_dropped() -> None:
    """A future validator code must fail loudly, not vanish from the editor."""
    with pytest.raises(ViewModelError, match="unmapped validation error code"):
        build_annotation(ValidationError(code="SOME_FUTURE_CODE", message="?", node_id="n"))
    with pytest.raises(ViewModelError, match="code"):
        build_annotation(ValidationError(code="", message="no code"))
    with pytest.raises(ViewModelError, match="code"):
        build_annotation(ValidationError(code="   ", message="blank code", node_id="n"))
    # ...and a batch containing one unknown code refuses rather than mapping
    # the rest and skipping it.
    with pytest.raises(ViewModelError):
        build_annotations(
            [
                ValidationError(code="NO_TRIGGER", message="m", node_id="n"),
                ValidationError(code="INVENTED_CODE", message="m", node_id="n"),
            ]
        )


@pytest.mark.unit
def test_annotations_extract_the_port_when_the_error_names_one() -> None:
    port_naming = {
        "UNCONNECTED_REQUIRED_INPUT": (
            "Required input port 'query' on node 'retriever' is not connected",
            "query",
        ),
        "DUPLICATE_PORT_NAME": ("Duplicate port name 'output' on node 'n1'", "output"),
        "DANGLING_SOURCE_PORT": ("Edge 'e1' leaves unknown source port 'out'", "out"),
    }
    for code, (message, expected_port) in port_naming.items():
        annotation = build_annotation(ValidationError(code=code, message=message, node_id="n1"))
        assert annotation.port == expected_port, code


@pytest.mark.unit
def test_annotations_do_not_guess_a_port() -> None:
    """A missing port is an honest annotation; a guessed one is a lie."""
    annotation = build_annotation(
        ValidationError(code="CYCLE_DETECTED", message="Cycle: a -> b -> a", node_id="a")
    )
    assert annotation.port is None


@pytest.mark.unit
def test_annotations_are_ordered_and_never_dropped() -> None:
    errors = [
        ValidationError(code="DANGLING_SOURCE", message="m1", node_id="b"),
        ValidationError(code="TYPE_MISMATCH", message="m2", node_id="a"),
        ValidationError(code="UNSUPPORTED_CAPABILITY", message="m3", node_id=None),
    ]
    annotations = build_annotations(errors)
    assert len(annotations) == 3  # every one mapped
    assert [a.code for a in annotations] == [
        "DANGLING_SOURCE",
        "TYPE_MISMATCH",
        "UNSUPPORTED_CAPABILITY",
    ]
    # A workflow-wide error has no node, which is honest.
    assert annotations[2].node_id is None


@pytest.mark.unit
def test_validation_feedback_summarises() -> None:
    ok = build_validation_feedback(ValidationResult(valid=True, errors=[]))
    assert isinstance(ok, ValidationFeedback)
    assert ok.valid and ok.annotations == [] and ok.blocking == []

    bad = build_validation_feedback(
        ValidationResult(
            valid=False,
            errors=[
                ValidationError(code="CYCLE_DETECTED", message="Cycle", node_id="a"),
                ValidationError(code="DUPLICATE_NODE_ID", message="Dup", node_id="b"),
            ],
        )
    )
    assert not bad.valid
    assert len(bad.blocking) == 2
    assert bad.error_count == 2 and bad.unmapped == []
    assert bad.warning is False


@pytest.mark.unit
def test_validation_feedback_is_json_serialisable() -> None:
    feedback = build_validation_feedback(
        ValidationResult(
            valid=False,
            errors=[ValidationError(code="NO_TRIGGER", message="m", node_id="n")],
        )
    )
    payload = json.loads(feedback.model_dump_json())
    assert payload["blocking"][0]["code"] == "NO_TRIGGER"
    assert payload["annotations"][0]["severity"] in {"error", "warning"}


# --------------------------------------------------------------------------- #
# 2. Run information dropped: 0
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_every_node_run_becomes_a_timeline_entry() -> None:
    runs = {
        "start": {"node_id": "start", "status": NodeRunStatus.COMPLETED, "output_data": {"x": 1}},
        "mid": {
            "node_id": "mid",
            "status": NodeRunStatus.COMPLETED,
            "output_data": "v",
            "attempts": 3,
            "latency_ms": 99.0,
        },
        "end": {"node_id": "end", "status": NodeRunStatus.COMPLETED, "output_data": "final"},
    }
    timeline = build_timeline(_run(node_runs=runs))
    assert [e.node_id for e in timeline.entries] == ["start", "mid", "end"]
    assert timeline.node_count == 3
    assert timeline.counts[TimelineEntryState.COMPLETED] == 3


@pytest.mark.unit
def test_node_details_survive_the_mapping() -> None:
    timeline = build_timeline(
        _run(
            node_runs={
                "mid": {
                    "node_id": "mid",
                    "status": NodeRunStatus.COMPLETED,
                    "output_data": "the answer",
                    "attempts": 3,
                    "latency_ms": 145.5,
                }
            }
        )
    )
    entry = timeline.entries[0]
    assert entry.attempts == 3
    assert entry.latency_ms == 145.5
    assert entry.produced_output is True
    assert "the answer" in (entry.output_summary or "")


@pytest.mark.unit
def test_skip_reasons_survive() -> None:
    runs = {
        "guard": {
            "node_id": "guard",
            "status": NodeRunStatus.SKIPPED,
            "error": "barrier-incomplete: upstream never arrived",
            "latency_ms": 0.0,
            "attempts": 0,
        },
        "down": {
            "node_id": "down",
            "status": NodeRunStatus.SKIPPED,
            "error": "upstream-skipped: skipped by upstream",
            "latency_ms": 0.0,
            "attempts": 0,
        },
    }
    timeline = build_timeline(_run(node_runs=runs))
    by_node = {e.node_id: e for e in timeline.entries}
    assert by_node["guard"].reason == "barrier-incomplete: upstream never arrived"
    assert by_node["down"].reason == "upstream-skipped: skipped by upstream"
    assert by_node["guard"].state == TimelineEntryState.SKIPPED


@pytest.mark.unit
def test_a_skip_with_no_reason_is_not_rendered_silently() -> None:
    """A skip is disclosed; a reason-less skip says so instead of hiding."""
    timeline = build_timeline(
        _run(node_runs={"n": {"node_id": "n", "status": NodeRunStatus.SKIPPED}})
    )
    assert timeline.entries[0].reason == "skipped (no reason recorded)"


@pytest.mark.unit
def test_terminal_state_is_prominent_and_names_its_cause() -> None:
    # Failed
    failed = build_timeline(
        _run(
            status=WorkflowRunStatus.FAILED,
            node_runs={"bad": {"node_id": "bad", "status": NodeRunStatus.FAILED, "error": "boom"}},
            failed_node_id="bad",
            error="node 'bad' failed",
        )
    )
    assert failed.state == TimelineTerminalState.FAILED
    assert failed.failed_node_id == "bad"
    assert failed.run_error == "node 'bad' failed"
    assert failed.entries[0].is_terminal_cause is True
    assert failed.final_output_summary is None  # a failed run has no output

    # Cancelled
    cancelled = build_timeline(
        _run(
            status=WorkflowRunStatus.CANCELLED,
            node_runs={
                "n": {
                    "node_id": "n",
                    "status": NodeRunStatus.SKIPPED,
                    "error": "cancelled: run cancelled",
                }
            },
            failed_node_id="n",
        )
    )
    assert cancelled.state == TimelineTerminalState.CANCELLED
    assert cancelled.failed_node_id == "n"
    assert cancelled.entries[0].is_terminal_cause is True


@pytest.mark.unit
def test_a_terminal_run_without_a_recorded_cause_is_disclosed() -> None:
    """Never render an unlabelled non-completed state."""
    timeline = build_timeline(_run(status=WorkflowRunStatus.FAILED, node_runs={}))
    assert timeline.state == TimelineTerminalState.FAILED
    assert timeline.failed_node_id is None
    assert timeline.run_error == "run ended without a recorded cause"


@pytest.mark.unit
def test_running_and_pending_render_as_running() -> None:
    for status in (WorkflowRunStatus.PENDING, WorkflowRunStatus.RUNNING):
        timeline = build_timeline(
            _run(status=status, node_runs={"n": {"node_id": "n", "status": NodeRunStatus.RUNNING}})
        )
        assert timeline.state == TimelineTerminalState.RUNNING
        assert timeline.entries[0].state == TimelineEntryState.RUNNING


@pytest.mark.unit
def test_completed_run_carries_final_output_and_duration() -> None:
    timeline = build_timeline(
        _run(
            node_runs={
                "n": {
                    "node_id": "n",
                    "status": NodeRunStatus.COMPLETED,
                    "output_data": "final result",
                }
            },
            final_output="final result",
        )
    )
    assert timeline.state == TimelineTerminalState.COMPLETED
    assert "final result" in (timeline.final_output_summary or "")
    assert timeline.duration_ms == 2000.0


@pytest.mark.unit
def test_declared_nodes_are_presented_even_when_not_reached() -> None:
    timeline = build_timeline(
        _run(node_runs={"a": {"node_id": "a", "status": NodeRunStatus.COMPLETED}}),
        definition_nodes=["a", "b", "c"],
    )
    assert [e.node_id for e in timeline.entries] == ["a", "b", "c"]
    assert timeline.entries[1].state == TimelineEntryState.PENDING
    assert timeline.entries[1].reason == "not reached"
    assert timeline.node_count == 3


@pytest.mark.unit
def test_counts_add_up_to_the_entry_count() -> None:
    runs = {
        "a": {"node_id": "a", "status": NodeRunStatus.COMPLETED},
        "b": {"node_id": "b", "status": NodeRunStatus.FAILED, "error": "x"},
        "c": {"node_id": "c", "status": NodeRunStatus.SKIPPED, "error": "s"},
    }
    timeline = build_timeline(_run(status=WorkflowRunStatus.FAILED, node_runs=runs))
    assert sum(timeline.counts.values()) == timeline.node_count == 3


@pytest.mark.unit
def test_an_unknown_status_is_refused() -> None:
    """A status outside the enum is refused by pydantic before we even map it.

    The map is total for the real statuses, so the only way to reach an
    unmapped one is a status that should not exist — and pydantic refuses it at
    construction, which is the earlier and better place to fail.
    """
    with pytest.raises(ValueError, match=r"pending|skipped|Input should be"):
        NodeRun(node_id="n", status="not_a_status")  # type: ignore[arg-type]
    # And the mapping table covers every real status, so nothing legitimately
    # maps to an unknown state.
    from workflow_engine.interfaces import NodeRunStatus
    from workflow_engine.view_models import _ENTRY_STATES

    assert set(_ENTRY_STATES) == set(NodeRunStatus)


@pytest.mark.unit
def test_timeline_is_json_serialisable() -> None:
    timeline = build_timeline(
        _run(
            node_runs={
                "n": {"node_id": "n", "status": NodeRunStatus.COMPLETED, "output_data": "ok"}
            }
        )
    )
    payload = json.loads(timeline.model_dump_json())
    assert payload["entries"][0]["node_id"] == "n"
    assert payload["state"] == "completed"
    assert isinstance(payload["counts"], dict)


@pytest.mark.unit
def test_every_real_skip_reason_round_trips() -> None:
    """The executor's skip reasons must survive the mapping unchanged."""
    executor = Path(__file__).resolve().parents[2] / "packages" / "workflow_engine" / "executor.py"
    source = executor.read_text(encoding="utf-8")
    reasons = set(re.findall(r"^_SKIP_[A-Z_]+ = \"([^\"]+)\"", source, re.MULTILINE))
    assert reasons, "no skip reasons found in the executor"
    for reason in reasons:
        timeline = build_timeline(
            _run(
                node_runs={
                    "n": {
                        "node_id": "n",
                        "status": NodeRunStatus.SKIPPED,
                        "error": f"{reason}: detail here",
                    }
                }
            )
        )
        assert timeline.entries[0].reason == f"{reason}: detail here"


@pytest.mark.unit
def test_a_real_run_from_the_executor_maps_with_nothing_dropped() -> None:
    """End-to-end: an actual executor run maps into a complete timeline."""
    import hashlib

    from workflow_engine import (
        WorkflowCompiler,
        WorkflowEdgeSpec,
        WorkflowExecutor,
        WorkflowRevision,
    )

    definition = WorkflowDefinition(
        id="wf-vm",
        workspace_id="ws-vm",
        name="VM",
        nodes=[
            NodeSpec(
                id="start",
                type=NodeType.TRIGGER,
                name="Start",
                output_ports=[PortSpec(name="output", type="dict")],
            ),
            NodeSpec(
                id="fmt",
                type=NodeType.TRANSFORM,
                name="Fmt",
                config={"template": "Hi {name}!"},
                input_ports=[PortSpec(name="input", type="any")],
                output_ports=[PortSpec(name="output", type="string")],
            ),
            NodeSpec(
                id="out",
                type=NodeType.OUTPUT,
                name="Out",
                input_ports=[PortSpec(name="input", type="any")],
                output_ports=[PortSpec(name="output", type="any")],
            ),
        ],
        edges=[
            WorkflowEdgeSpec(
                id="e1",
                source_node_id="start",
                source_port="output",
                target_node_id="fmt",
                target_port="input",
            ),
            WorkflowEdgeSpec(
                id="e2",
                source_node_id="fmt",
                source_port="output",
                target_node_id="out",
                target_port="input",
            ),
        ],
    )
    canonical = json.dumps(definition.model_dump(mode="json"), sort_keys=True)
    revision = WorkflowRevision(
        workflow_id=definition.id,
        revision=1,
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        spec=definition,
    )
    plan = WorkflowCompiler.compile(revision, allow_unvalidated=True)
    executor = WorkflowExecutor(FakeNodeExecutor())
    run = asyncio.run(executor.execute(plan, revision, {"name": "Ada"}))

    timeline = build_timeline(run, definition_nodes=[n.id for n in definition.nodes])
    assert timeline.state == TimelineTerminalState.COMPLETED
    assert timeline.node_count == 3
    assert {e.node_id for e in timeline.entries} == {"start", "fmt", "out"}
    assert timeline.counts[TimelineEntryState.COMPLETED] == 3
    assert timeline.final_output_summary is not None


# --------------------------------------------------------------------------- #
# 3. UI code added: 0
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_no_ui_code_in_the_view_models() -> None:
    """Card metric: UI code added = 0 (AST-based, as in T-V4-022)."""
    tree = ast.parse(VIEW_MODELS.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")

    ui_roots = {"react", "react-dom", "next", "framer-motion", "clsx", "zustand"}
    assert not (ui_roots & imports), imports

    ui_names = {
        "onClick",
        "onChange",
        "onDragStart",
        "useState",
        "useEffect",
        "useMemo",
        "forwardRef",
        "jsx",
        "canvas",
        "render",
        "Component",
        "div",
        "span",
    }
    referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not (ui_names & referenced), ui_names & referenced


@pytest.mark.unit
def test_no_ui_files_were_added_by_this_ticket() -> None:
    out = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    ui_like = [
        line
        for line in out
        if line.strip().endswith((".tsx", ".jsx")) or "/components/" in line or "/app/" in line
    ]
    assert ui_like == [], f"UI files changed in the tree: {ui_like}"


@pytest.mark.unit
def test_both_models_are_json_serialisable() -> None:
    """A canvas consumes data, rendered unchanged."""
    annotation = build_annotation(ValidationError(code="NO_TRIGGER", message="m", node_id="n"))
    assert json.loads(annotation.model_dump_json())["code"] == "NO_TRIGGER"
    timeline = build_timeline(_run(node_runs={}))
    assert json.loads(timeline.model_dump_json())["run_id"] == "run-1"


@pytest.mark.unit
def test_models_reject_unknown_fields() -> None:
    """extra='forbid' at the view-model boundary, so a canvas never silently
    drops a field it was handed (and a builder never adds one nobody reads)."""
    with pytest.raises(ValueError, match="invented"):
        EditorAnnotation.model_validate(
            {
                "code": "NO_TRIGGER",
                "message": "m",
                "severity": AnnotationSeverity.ERROR,
                "invented": True,
            }
        )
    with pytest.raises(ValueError, match="bogus"):
        TimelineEntry.model_validate(
            {"node_id": "n", "state": TimelineEntryState.COMPLETED, "bogus": 1}
        )
