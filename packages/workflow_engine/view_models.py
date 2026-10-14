"""Editor view models for validator feedback and run state (T-V4-023 / W5).

Pure builders: validator errors to placeable editor annotations, and a
:class:`~workflow_engine.interfaces.WorkflowRun` to a timeline model. Both are
**data** — no UI component, no canvas code, no event handler. The editor renders
these models unchanged; that is the point.

What the builders guarantee
---------------------------
* **No validation error is dropped.** Every :class:`ValidationError` the real
  validator emits becomes an annotation carrying node id, port (when the error
  names one), the message and a severity. The mapping table below is total: a
  code that is not in it is refused rather than silently skipped, so an
  unrecognised future error code fails a test instead of vanishing from the
  canvas.
* **No run information is dropped.** Every node run, the run's terminal state,
  every skip reason, the failing node and the timing all survive into the
  timeline model. A timeline entry exists for every key in ``node_runs`` — no
  node is quietly missing — and the terminal state is carried explicitly with
  the node that caused it, so a cancelled run is never rendered as an endless
  in-progress one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .interfaces import (
    NodeRun,
    NodeRunStatus,
    ValidationError,
    ValidationResult,
    WorkflowRun,
    WorkflowRunStatus,
)

# --------------------------------------------------------------------------- #
# Shared vocabulary
# --------------------------------------------------------------------------- #

#: A non-blank, bounded identifier.
NonBlankId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]

#: Maximum characters of a message the editor shows before it wraps it itself.
MAX_MESSAGE_CHARS = 1_024


class AnnotationSeverity(StrEnum):
    """How the editor presents a validation problem."""

    ERROR = "error"  # blocks the workflow from being saved or run
    WARNING = "warning"  # the workflow runs, but something needs attention


class TimelineEntryState(StrEnum):
    """The editor's view of one node run. (Mirrors NodeRunStatus, plus one.)

    ``SKIPPED`` is first-class because a skip is a disclosed fact the engine
    records a reason for, never an absent node.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TimelineTerminalState(StrEnum):
    """The run's terminal state, made prominent in the timeline."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ViewModelError(ValueError):
    """A view model could not be built from its input (never silently fudged)."""


# --------------------------------------------------------------------------- #
# 1. Validator feedback -> editor annotations
# --------------------------------------------------------------------------- #


class EditorAnnotation(BaseModel):
    """One placeable editor annotation: where the problem is and how bad it is."""

    model_config = ConfigDict(extra="forbid")

    #: The validator's stable machine-readable code.
    code: str = Field(min_length=1, max_length=64)
    #: The node the annotation attaches to; ``None`` for workflow-wide errors.
    node_id: str | None = Field(default=None, max_length=128)
    #: The port the annotation attaches to; ``None`` when the error does not
    #: name one.
    port: str | None = Field(default=None, max_length=128)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    severity: AnnotationSeverity
    #: True when the annotation makes the workflow unsaveable/unrunnable.
    blocking: bool = True


#: Every code the real validator emits, mapped to (severity, blocking).
#: A TOTAL table — :func:`build_annotations` refuses a code that is not here.
_ANNOTATION_TABLE: dict[str, tuple[AnnotationSeverity, bool]] = {
    # Structural integrity: the workflow cannot run.
    "EMPTY_WORKFLOW": (AnnotationSeverity.ERROR, True),
    "NO_TRIGGER": (AnnotationSeverity.ERROR, True),
    "NO_OUTPUT_NODE": (AnnotationSeverity.ERROR, True),
    "DUPLICATE_NODE_ID": (AnnotationSeverity.ERROR, True),
    "DUPLICATE_EDGE_ID": (AnnotationSeverity.ERROR, True),
    "DUPLICATE_PORT_NAME": (AnnotationSeverity.ERROR, True),
    "EMPTY_PORT_NAME": (AnnotationSeverity.ERROR, True),
    "CYCLE_DETECTED": (AnnotationSeverity.ERROR, True),
    "ORPHANED_NODE": (AnnotationSeverity.ERROR, True),
    "DANGLING_SOURCE": (AnnotationSeverity.ERROR, True),
    "DANGLING_TARGET": (AnnotationSeverity.ERROR, True),
    "DANGLING_SOURCE_PORT": (AnnotationSeverity.ERROR, True),
    "DANGLING_TARGET_PORT": (AnnotationSeverity.ERROR, True),
    # Wiring and typing: the workflow cannot run as drawn.
    "UNCONNECTED_REQUIRED_INPUT": (AnnotationSeverity.ERROR, True),
    "TYPE_MISMATCH": (AnnotationSeverity.ERROR, True),
    "CONDITION_EDGE_NO_VALUE": (AnnotationSeverity.ERROR, True),
    # Capability and placement: the workflow cannot run HERE.
    "UNSUPPORTED_NODE_TYPE": (AnnotationSeverity.ERROR, True),
    "UNSUPPORTED_CAPABILITY": (AnnotationSeverity.ERROR, True),
    # Resource bounds: refused before the quadratic checks run.
    "TOO_MANY_NODES": (AnnotationSeverity.ERROR, True),
    "TOO_MANY_EDGES": (AnnotationSeverity.ERROR, True),
    "NODE_FAN_OUT_TOO_HIGH": (AnnotationSeverity.ERROR, True),
    "NODE_CONFIG_TOO_LARGE": (AnnotationSeverity.ERROR, True),
    "GRAPH_CONFIG_TOO_LARGE": (AnnotationSeverity.ERROR, True),
}


def _port_from_error(error: ValidationError) -> str | None:
    """Extract the port name an error names, if any.

    The validator writes port names into its messages in a few shapes:
    ``Required input port 'query' …``, ``Duplicate port name 'output' …``,
    ``Edge 'e1' leaves unknown source port 'out'``. The quoted token after a
    port-bearing phrase is the port. When the message names no port, the answer
    is ``None`` — a missing port is an honest annotation, a guessed one is a
    lie about where the problem is.
    """
    message = error.message or ""
    for marker in (
        "input port '",
        "output port '",
        "port name '",
        "source port '",
        "target port '",
        "port '",
    ):
        if marker in message:
            tail = message.split(marker, 1)[1]
            return tail.split("'", 1)[0] or None
    return None


def build_annotation(error: ValidationError) -> EditorAnnotation:
    """Map one :class:`ValidationError` to an editor annotation.

    An unrecognised code raises: the alternative is a validator error the editor
    never shows, which is exactly how a user ships a workflow the engine
    rejects. New validator codes must be added to :data:`_ANNOTATION_TABLE`.
    """
    code = (error.code or "").strip()
    if not code:
        raise ViewModelError("a validation error without a code cannot be shown")
    try:
        severity, blocking = _ANNOTATION_TABLE[code]
    except KeyError as exc:
        raise ViewModelError(
            f"unmapped validation error code {code!r}; refusing to drop it from "
            "the editor — add it to _ANNOTATION_TABLE"
        ) from exc
    message = (error.message or "").strip() or code
    return EditorAnnotation(
        code=code,
        node_id=error.node_id,
        port=_port_from_error(error),
        message=message[:MAX_MESSAGE_CHARS],
        severity=severity,
        blocking=blocking,
    )


def build_annotations(
    errors: Iterable[ValidationError] | ValidationResult,
) -> list[EditorAnnotation]:
    """Map every validation error, in order, refusing to drop any."""
    if isinstance(errors, ValidationResult):
        errors = errors.errors
    return [build_annotation(error) for error in errors]


def build_validation_feedback(
    result: ValidationResult,
) -> ValidationFeedback:
    """Summarise a validation result for the editor: annotations + verdict."""
    annotations = build_annotations(result.errors)
    blocking = [a for a in annotations if a.blocking]
    return ValidationFeedback(
        valid=result.valid,
        annotations=annotations,
        blocking=blocking,
        warning=not result.valid and not blocking,
        error_count=len([a for a in annotations if a.severity == AnnotationSeverity.ERROR]),
        warning_count=len([a for a in annotations if a.severity == AnnotationSeverity.WARNING]),
        unmapped=[],
    )


class ValidationFeedback(BaseModel):
    """What the editor shows after a validation pass."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    annotations: list[EditorAnnotation] = Field(default_factory=list)
    #: Only the annotations that block saving/running.
    blocking: list[EditorAnnotation] = Field(default_factory=list)
    #: True when the result is invalid for a non-blocking reason.
    warning: bool = False
    error_count: int = 0
    warning_count: int = 0
    #: Codes seen but not mapped. Always empty here: unmapped codes RAISE
    #: rather than land in this list, which is what keeps it honest. (An empty
    #: list, not an absent field, so a client can always iterate it.)
    unmapped: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 2. Run state -> timeline model
# --------------------------------------------------------------------------- #


class TimelineEntry(BaseModel):
    """One node in the timeline: status, timing, and why it is not completed."""

    model_config = ConfigDict(extra="forbid")

    node_id: NonBlankId
    state: TimelineEntryState
    #: Ordering key — the node's position in the run (insertion order).
    sequence: int = Field(ge=0)
    #: Milliseconds the node took. 0.0 for a node that never executed.
    latency_ms: float = Field(ge=0.0)
    #: Bounded executions the node took (1..N for a retried node).
    attempts: int = Field(ge=0)
    #: The node's output when it completed. ``None`` otherwise — the timeline
    #: carries the output but never invents one.
    output_summary: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    #: The failure reason (FAILED) or the disclosed skip reason (SKIPPED).
    reason: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    #: True for the node that made the run terminal.
    is_terminal_cause: bool = False
    #: Whether the node produced output at all (completed with data).
    produced_output: bool = False


class RunTimeline(BaseModel):
    """The run as the editor shows it: ordered entries + a prominent terminal."""

    model_config = ConfigDict(extra="forbid")

    run_id: NonBlankId
    workflow_id: NonBlankId
    workspace_id: NonBlankId
    state: TimelineTerminalState
    #: Entries in run order (node_runs insertion order).
    entries: list[TimelineEntry] = Field(default_factory=list)
    #: Total nodes the run knows about — must equal len(entries).
    node_count: int = Field(ge=0)
    #: Counts by state, so a canvas can render a distribution without scanning.
    counts: dict[TimelineEntryState, int] = Field(default_factory=dict)
    #: The node that made the run terminal (failure, incomplete barrier, or
    #: cancellation) — never absent for a non-COMPLETED, non-running run.
    failed_node_id: str | None = Field(default=None, max_length=128)
    #: The run's own error (disclosed, never swallowed).
    run_error: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    #: The run's final output when it completed.
    final_output_summary: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    created_at: str = ""
    finished_at: str | None = None
    #: Milliseconds of wall time the run took, when it finished.
    duration_ms: float | None = Field(default=None, ge=0.0)

    @field_validator("counts", mode="before")
    @classmethod
    def _keys_as_states(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        normalised: dict[str, int] = {}
        for key, count in value.items():
            normalised[str(getattr(key, "value", key))] = count
        return normalised


#: NodeRunStatus -> TimelineEntryState. A total mapping: a status with no entry
#: is refused rather than rendered as an exotic unknown state.
_ENTRY_STATES: dict[NodeRunStatus, TimelineEntryState] = {
    NodeRunStatus.PENDING: TimelineEntryState.PENDING,
    NodeRunStatus.RUNNING: TimelineEntryState.RUNNING,
    NodeRunStatus.COMPLETED: TimelineEntryState.COMPLETED,
    NodeRunStatus.FAILED: TimelineEntryState.FAILED,
    NodeRunStatus.SKIPPED: TimelineEntryState.SKIPPED,
}

#: WorkflowRunStatus -> TimelineTerminalState.
_TERMINAL_STATES: dict[WorkflowRunStatus, TimelineTerminalState] = {
    WorkflowRunStatus.PENDING: TimelineTerminalState.RUNNING,
    WorkflowRunStatus.RUNNING: TimelineTerminalState.RUNNING,
    WorkflowRunStatus.COMPLETED: TimelineTerminalState.COMPLETED,
    WorkflowRunStatus.FAILED: TimelineTerminalState.FAILED,
    WorkflowRunStatus.CANCELLED: TimelineTerminalState.CANCELLED,
}


def _summary(value: object) -> str | None:
    """A bounded one-line summary of an output value for the timeline.

    Returns ``None`` when there is nothing to summarise: an absent output is
    rendered as absent, not as "null".
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else repr(value)
    text = text.strip()
    return text[:MAX_MESSAGE_CHARS] or None


def build_timeline_entry(
    node_run: NodeRun, *, sequence: int, is_terminal_cause: bool = False
) -> TimelineEntry:
    """Map one :class:`NodeRun` to a timeline entry, keeping everything it says."""
    try:
        state = _ENTRY_STATES[node_run.status]
    except KeyError as exc:
        raise ViewModelError(
            f"unknown node run status {node_run.status!r}; refusing to render it "
            "as an unknown state"
        ) from exc

    reason = (node_run.error or "").strip() or None
    if state == TimelineEntryState.SKIPPED and not reason:
        # A skip with no recorded reason is a lie by omission: the engine
        # always records one, so a missing reason means something upstream
        # broke — say so rather than rendering a silent skip.
        reason = "skipped (no reason recorded)"
    produced = state == TimelineEntryState.COMPLETED and node_run.output_data is not None

    return TimelineEntry(
        node_id=node_run.node_id,
        state=state,
        sequence=sequence,
        latency_ms=float(node_run.latency_ms or 0.0),
        attempts=int(node_run.attempts or 0),
        output_summary=_summary(node_run.output_data) if produced else None,
        reason=reason,
        is_terminal_cause=is_terminal_cause,
        produced_output=produced,
    )


def build_timeline(run: WorkflowRun, *, definition_nodes: Sequence[str] = ()) -> RunTimeline:
    """Build the timeline model for *run*.

    ``definition_nodes`` is optional: when supplied, every node in the
    definition appears even if the run never recorded it (a not-yet-reached
    node becomes a pending entry), so the editor's node list never depends on
    how far the run happened to get.
    """
    try:
        state = _TERMINAL_STATES[run.status]
    except KeyError as exc:
        raise ViewModelError(f"unknown run status {run.status!r}; refusing to render it") from exc

    cause = run.failed_node_id
    entries: list[TimelineEntry] = []
    seen: set[str] = set()
    for sequence, (node_id, node_run) in enumerate(run.node_runs.items()):
        entries.append(
            build_timeline_entry(node_run, sequence=sequence, is_terminal_cause=node_id == cause)
        )
        seen.add(node_id)

    # Nodes the definition declares but the run never reached.
    for offset, node_id in enumerate(definition_nodes):
        if node_id in seen:
            continue
        seen.add(node_id)
        entries.append(
            TimelineEntry(
                node_id=node_id,
                state=TimelineEntryState.PENDING,
                sequence=len(run.node_runs) + offset,
                latency_ms=0.0,
                attempts=0,
                reason="not reached",
            )
        )

    counts: dict[TimelineEntryState, int] = {}
    for entry in entries:
        counts[entry.state] = counts.get(entry.state, 0) + 1

    duration_ms = None
    if run.finished_at and run.created_at:
        duration_ms = _elapsed_ms(run.created_at, run.finished_at)

    # A non-running terminal run must name its cause; a cancelled one names the
    # node the cancellation stopped at. If it does not, the timeline says so
    # instead of pretending the cause is absent.
    run_error = (run.error or "").strip() or None
    if state in {TimelineTerminalState.FAILED, TimelineTerminalState.CANCELLED}:
        if not cause and not run_error:
            run_error = "run ended without a recorded cause"

    return RunTimeline(
        run_id=run.id,
        workflow_id=run.workflow_id,
        workspace_id=run.workspace_id,
        state=state,
        entries=entries,
        node_count=len(entries),
        counts=counts,
        failed_node_id=cause,
        run_error=run_error,
        final_output_summary=_summary(run.final_output)
        if state == TimelineTerminalState.COMPLETED
        else None,
        created_at=run.created_at,
        finished_at=run.finished_at,
        duration_ms=duration_ms,
    )


def _elapsed_ms(start: str, end: str) -> float | None:
    """Milliseconds between two ISO timestamps, or ``None`` if unparseable."""
    from datetime import datetime

    def _parse(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    a, b = _parse(start), _parse(end)
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds()
    return max(0.0, delta * 1000.0)


__all__ = [
    "AnnotationSeverity",
    "EditorAnnotation",
    "RunTimeline",
    "TimelineEntry",
    "TimelineEntryState",
    "TimelineTerminalState",
    "ValidationFeedback",
    "ViewModelError",
    "build_annotation",
    "build_annotations",
    "build_timeline",
    "build_timeline_entry",
    "build_validation_feedback",
]
