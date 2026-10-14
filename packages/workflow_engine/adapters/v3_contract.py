"""V3 execution-adapter seam: the production bridge's CONTRACT (T-V4-014).

This module is the contract the future production ``AgentRunner`` bridge must
satisfy. It is **types and rules only**:

* no bridge callable is built here;
* nothing imports ``packages.agents`` (V3) — the taxonomy below is *declared*
  as literals so the workflow engine keeps its independence from V3 and the
  existing flagged-off W3 adapter (``v3_adapter.py``) is untouched;
* no behaviour change: the ``AgentRunner`` Protocol in ``v3_adapter`` keeps its
  permissive shape, and this contract is a stricter layer a bridge may choose to
  satisfy.

What the contract fixes
-----------------------
``v3_adapter`` currently hands the V3 authority a plain ``dict`` and returns an
unvalidated mapping. That is a working seam but not a *checkable* one: a bridge
that misspelled a request key, returned a status the executor cannot interpret,
or swallowed a cancellation would look like success. So this module declares:

1. **Request shape** — :class:`V3NodeRequest`: the 6 keys ``v3_adapter`` sends,
   each typed and bounded, with ``node_id``/``workspace_id`` non-blank.
2. **Response shape** — :class:`V3NodeResponse`: ``status`` (a terminal V3
   outcome), ``output_data`` (the node's output-port mapping), and optional
   ``error`` (mapped, never a raw string).
3. **Capability manifest mapping** — :class:`V3CapabilityMapping` + :func:`map_manifest`:
   the workflow manifest's ``enabled_capabilities`` to the set a V3 run may
   declare, with an explicit entry for every declared capability.
4. **Error taxonomy mapping** — :class:`V3Error` + :func:`map_error`: every V3
   error code to a workflow terminal outcome with the node named. In particular
   ``CANCELLED`` maps to workflow ``CANCELLED`` (never ``FAILED``), and no
   code maps to a swallowed/success outcome.
5. **Timeout / cancel propagation** — :class:`V3DispatchLimits` + :func:`validate_dispatch`:
   a dispatch is refused when its timeout exceeds the deadline, and cancellation
   is propagated to the workflow as a named cancelled node.

The rule this exists to enforce: **an unmapped or unrecognised V3 outcome is a
refusal, never a silent success.** A `None` result, an unknown status, or an
error the taxonomy does not know must fail loudly rather than degrade into "the
node ran and produced nothing".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ..interfaces import CapabilityManifest, NodeSpec, NodeType

# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #

#: Maximum nested input payload a request may carry (bytes of JSON).
MAX_REQUEST_BYTES = 262_144
#: Maximum length of a node or workspace identifier.
MAX_ID_CHARS = 128
#: Maximum capabilities one request may declare.
MAX_CAPABILITIES = 32
#: Maximum characters of an error message carried across the seam.
MAX_ERROR_MESSAGE_CHARS = 2_048
#: Minimum timeout a dispatch may request (seconds).
MIN_TIMEOUT_SECONDS = 0.01
#: Maximum timeout a dispatch may request (seconds).
MAX_TIMEOUT_SECONDS = 3_600.0

#: A non-blank, bounded identifier.
NonBlankId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ID_CHARS)
]


# --------------------------------------------------------------------------- #
# V3 outcome vocabulary — DECLARED here, not imported from packages.agents
# --------------------------------------------------------------------------- #


class V3RunStatus(StrEnum):
    """Terminal V3 run statuses the workflow surface understands.

    Mirrors ``packages.agents.interfaces.AgentRunStatus`` **minus** the two
    non-terminal members (``pending``, ``running``) — declared by value so a V3
    rename fails this contract's tests rather than drifting silently.

    Note what is *not* here: ``cancelled``. Live V3 has no ``cancelled`` run
    status; cancellation arrives as the ``cancelled`` **error code** (see
    :class:`V3ErrorCode`) or on an interrupted run, and this contract carries it
    on the explicit :attr:`V3NodeResponse.cancelled` flag instead. Inventing a
    ``CANCELLED`` status would have made this contract unfulfillable by a real
    bridge, which the vocabulary test caught.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    BUDGET_EXCEEDED = "budget_exceeded"


class V3ErrorCode(StrEnum):
    """V3's stable error taxonomy (``packages.agents.AgentErrorCode``), by value.

    A member that is *not* in this enum is unmapped by definition, and
    :func:`map_error` refuses it rather than guessing.
    """

    TOOL_NOT_ALLOWED = "tool_not_allowed"
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_OUTPUT_TOO_LARGE = "tool_output_too_large"
    TOOL_SCHEMA_INVALID = "tool_schema_invalid"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    # T-R02: unconfigured execution (coordinator addendum, owner-prescribed).
    UNAVAILABLE = "unavailable"
    PROVIDER_ERROR = "provider_error"
    SCHEMA_INVALID = "schema_invalid"
    WORKSPACE_MISMATCH = "workspace_mismatch"
    SOURCE_REVOKED = "source_revoked"
    UNKNOWN_REQUIRED = "unknown_required"


#: What a V3 outcome means for the workflow node.
WorkflowOutcome = Literal["succeeded", "failed", "cancelled"]


class V3ContractError(ValueError):
    """A request/response/limits object violates the V3 seam contract."""


# --------------------------------------------------------------------------- #
# 1. Request shape
# --------------------------------------------------------------------------- #


class V3NodeRequest(BaseModel):
    """What the workflow hands the V3 authority for one node.

    These are exactly the six keys ``v3_adapter.V3NodeExecutor`` builds; the
    contract types them so a bridge can rely on their shape and the executor
    can validate what comes back. ``extra`` fields are refused rather than
    ignored, so a renamed key fails at the boundary instead of arriving as an
    absent field.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: NonBlankId
    node_type: str = Field(min_length=1, max_length=64)
    node_config: dict[str, object] = Field(default_factory=dict)
    workspace_id: NonBlankId
    input_data: dict[str, object] = Field(default_factory=dict)
    required_capabilities: list[NonBlankId] = Field(default_factory=list)

    @field_validator("node_type")
    @classmethod
    def _known_node_type(cls, value: str) -> str:
        if value not in {t.value for t in NodeType}:
            raise ValueError(f"{value!r} is not a workflow node type")
        return value

    @field_validator("required_capabilities")
    @classmethod
    def _capabilities_bounded(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_CAPABILITIES:
            raise ValueError(
                f"a request may declare at most {MAX_CAPABILITIES} capabilities"
            )
        if len(set(value)) != len(value):
            raise ValueError("capabilities must be unique")
        return value

    @model_validator(mode="after")
    def _within_payload_bound(self) -> V3NodeRequest:
        """Refuse an oversized request; never silently truncate it.

        Truncating would change what the V3 authority is asked to do, so an
        oversized request is a refusal at the boundary.
        """
        if self.json_size() > MAX_REQUEST_BYTES:
            raise ValueError(
                f"request serialises to more than {MAX_REQUEST_BYTES} bytes"
            )
        return self

    @classmethod
    def from_node(
        cls, node: NodeSpec, input_data: dict[str, object], workspace_id: str
    ) -> V3NodeRequest:
        """Build the request for *node* exactly as ``v3_adapter`` does."""
        return cls.model_validate(
            {
                "node_id": node.id,
                "node_type": str(node.type),
                "node_config": dict(node.config),
                "workspace_id": workspace_id,
                "input_data": input_data,
                "required_capabilities": list(node.required_capabilities),
            }
        )

    def json_size(self) -> int:
        """Serialized size of the request, for the payload bound."""
        import json

        return len(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, default=str)
            .encode("utf-8")
        )


# --------------------------------------------------------------------------- #
# 2. Response shape
# --------------------------------------------------------------------------- #


class V3ErrorDetail(BaseModel):
    """A V3 error as it may cross the seam: a **mapped** code, never free text."""

    model_config = ConfigDict(extra="forbid")

    code: V3ErrorCode
    message: str = Field(default="", max_length=MAX_ERROR_MESSAGE_CHARS)


class V3NodeResponse(BaseModel):
    """What the V3 authority must return for one node."""

    model_config = ConfigDict(extra="forbid")

    status: V3RunStatus
    output_data: dict[str, object] = Field(default_factory=dict)
    error: V3ErrorDetail | None = None
    #: True when the run was cancelled by a deadline or an explicit cancel.
    cancelled: bool = False

    @field_validator("error")
    @classmethod
    def _error_present_only_when_not_success(
        cls, value: V3ErrorDetail | None, info
    ) -> V3ErrorDetail | None:
        """A succeeded response may not carry an error (that is a contradiction)."""
        if value is not None and info.data.get("status") == V3RunStatus.COMPLETED:
            raise ValueError("a completed response must not carry an error")
        return value

    @field_validator("status")
    @classmethod
    def _terminal_status_only(cls, value: V3RunStatus) -> V3RunStatus:
        """Only terminal outcomes cross the seam.

        A V3 run that is still PENDING or RUNNING is not a result; a bridge
        that relays one would look like a node that produced nothing.
        """
        if value not in set(V3RunStatus):
            raise ValueError(f"{value!r} is not a terminal V3 outcome")
        return value

    @model_validator(mode="after")
    def _cancelled_flag_agrees_with_status(self) -> V3NodeResponse:
        """The ``cancelled`` flag, the status and any error must agree.

        Without this, a bridge could report a cancelled run as a failure (or a
        failure as a cancellation) simply by setting one field and not the
        others. The rules, given live V3 has no cancelled run status:

        * a cancelled response carries the ``cancelled`` error code, since that
          is how V3 reports cancellation;
        * a non-cancelled response must not set the flag;
        * a completed response carries no error at all.
        """
        if self.cancelled:
            if self.error is None:
                raise ValueError("a cancelled response must carry the cancelled error")
            if self.error.code != V3ErrorCode.CANCELLED:
                raise ValueError(
                    "a cancelled response must carry the CANCELLED error code, "
                    f"got {self.error.code.value!r}"
                )
        else:
            if self.status == V3RunStatus.FAILED and self.error is None:
                raise ValueError(
                    "a failed response must carry an error explaining the failure"
                )
        return self

    def workflow_outcome(self) -> WorkflowOutcome:
        """Which workflow outcome this response means (succeeded/failed/cancelled).

        Cancellation is read from the flag, not from a status — because V3 has
        no cancelled run status. A cancelled run is never a failure and never a
        success, whichever status the bridge relayed alongside the flag.
        """
        if self.cancelled:
            return "cancelled"
        if self.status == V3RunStatus.COMPLETED:
            return "succeeded"
        return "failed"


# --------------------------------------------------------------------------- #
# 3. Capability manifest mapping
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V3CapabilityMapping:
    """The capability decision for one node, with the reason recorded."""

    allowed: bool
    #: Capabilities the node requires that the mapping does not grant.
    missing: tuple[str, ...] = ()
    #: Capabilities the mapping grants that the node did not ask for.
    unused: tuple[str, ...] = ()


def map_manifest(manifest: CapabilityManifest, node: NodeSpec) -> V3CapabilityMapping:
    """Map a workflow capability manifest onto what a V3 run may declare.

    Refuses when the manifest enables nothing at all (a V3 run cannot be gated
    by an empty manifest — every capability check would pass vacuously, which
    is the same false-success shape as a disabled adapter that returns success).
    """
    enabled = set(manifest.enabled_capabilities)
    if not enabled:
        raise V3ContractError(
            "cannot map an empty capability manifest: every V3 capability check "
            "would pass vacuously (available: none)"
        )
    required = list(node.required_capabilities)
    missing = tuple(sorted(cap for cap in required if cap not in enabled))
    unused = tuple(sorted(cap for cap in enabled if cap not in required))
    return V3CapabilityMapping(
        allowed=not missing, missing=missing, unused=unused
    )


def assert_capability_allowed(
    manifest: CapabilityManifest, node: NodeSpec
) -> V3CapabilityMapping:
    """Raise unless *node*'s capabilities are granted by *manifest*."""
    mapping = map_manifest(manifest, node)
    if not mapping.allowed:
        raise V3ContractError(
            f"node {node.id!r} requires unavailable capabilities "
            f"{list(mapping.missing)} (available: {sorted(manifest.enabled_capabilities)})"
        )
    return mapping


# --------------------------------------------------------------------------- #
# 4. Error taxonomy mapping — V3 error -> workflow outcome, node always named
# --------------------------------------------------------------------------- #

#: V3 error code -> workflow outcome. CANCELLED maps to cancelled (never
#: failed), and budget/step limits are genuine failures.
_ERROR_OUTCOMES: dict[V3ErrorCode, WorkflowOutcome] = {
    V3ErrorCode.TOOL_NOT_ALLOWED: "failed",
    V3ErrorCode.TOOL_NOT_FOUND: "failed",
    V3ErrorCode.TOOL_TIMEOUT: "failed",
    V3ErrorCode.TOOL_OUTPUT_TOO_LARGE: "failed",
    V3ErrorCode.TOOL_SCHEMA_INVALID: "failed",
    V3ErrorCode.BUDGET_EXCEEDED: "failed",
    # A deadline exceeded is the *caller* running out of patience, not the node
    # failing: from the workflow's side the node stopped for a reason outside
    # the run, so its partial output is not a result. See
    # :func:`propagate_cancellation`.
    V3ErrorCode.DEADLINE_EXCEEDED: "cancelled",
    V3ErrorCode.CANCELLED: "cancelled",
    # T-R02: unconfigured execution fails the node (never success, never a new
    # outcome) — consistent with the FAILED-not-new-status decision.
    V3ErrorCode.UNAVAILABLE: "failed",
    V3ErrorCode.PROVIDER_ERROR: "failed",
    V3ErrorCode.SCHEMA_INVALID: "failed",
    V3ErrorCode.WORKSPACE_MISMATCH: "failed",
    V3ErrorCode.SOURCE_REVOKED: "failed",
    V3ErrorCode.UNKNOWN_REQUIRED: "failed",
}

#: V3 run status -> workflow outcome, for responses that carry no error object.
#: Cancellation is deliberately absent: there is no cancelled run *status* in
#: V3, so it is carried by the ``cancelled`` flag and handled in
#: :func:`map_response`.
_STATUS_OUTCOMES: dict[V3RunStatus, WorkflowOutcome] = {
    V3RunStatus.COMPLETED: "succeeded",
    V3RunStatus.FAILED: "failed",
    V3RunStatus.MAX_STEPS_EXCEEDED: "failed",
    V3RunStatus.BUDGET_EXCEEDED: "failed",
}


@dataclass(frozen=True)
class V3MappedError:
    """A V3 error after mapping: outcome, node id, and the original code."""

    code: V3ErrorCode
    outcome: WorkflowOutcome
    node_id: str
    message: str = ""


def map_error(error: V3ErrorDetail, *, node_id: str) -> V3MappedError:
    """Map *error* to a workflow outcome for the node named ``node_id``.

    The node id is mandatory: an unmapped error must still name the node it
    came from, which is what stops a swallowed failure from looking like a node
    that never ran. An unknown code is a refusal, never a default outcome.
    """
    if not node_id or not node_id.strip():
        raise V3ContractError("an error must name the node it came from")
    try:
        outcome = _ERROR_OUTCOMES[error.code]
    except KeyError as exc:
        raise V3ContractError(
            f"unmapped V3 error code {error.code!r}; refusing to guess an outcome"
        ) from exc
    return V3MappedError(
        code=error.code, outcome=outcome, node_id=node_id, message=error.message
    )


def map_response(
    response: V3NodeResponse, *, node_id: str
) -> tuple[WorkflowOutcome, V3MappedError | None]:
    """Map a whole response to ``(outcome, mapped_error)``.

    A ``CANCELLED`` status or ``cancelled=True`` yields ``cancelled``; any
    other terminal status yields ``failed``; ``COMPLETED`` yields ``succeeded``.
    """
    if response.status == V3RunStatus.COMPLETED and response.error is not None:
        raise V3ContractError("a completed response must not carry an error")
    if response.error is not None:
        mapped = map_error(response.error, node_id=node_id)
        # The flag wins over any status: a run the caller stopped is not a
        # failure, and it must not be recorded as one.
        if response.cancelled:
            return "cancelled", mapped
        return mapped.outcome, mapped
    try:
        outcome = _STATUS_OUTCOMES[response.status]
    except KeyError as exc:
        raise V3ContractError(
            f"unmapped V3 run status {response.status!r}; refusing to guess"
        ) from exc
    return outcome, None


# --------------------------------------------------------------------------- #
# 5. Timeout / cancel propagation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V3DispatchLimits:
    """Deadline and cancellation rules for one dispatch."""

    #: Seconds the V3 run may take before it is cancelled.
    timeout_seconds: float = 60.0
    #: Whether the caller may cancel the run mid-flight.
    cancellable: bool = True


def validate_dispatch(limits: V3DispatchLimits) -> V3DispatchLimits:
    """Validate dispatch limits, refusing contradictory or unsafe ones.

    A timeout beyond the maximum is refused rather than clamped: a clamp would
    let a bridge keep running after the workflow gave up, which is exactly how
    a cancelled run silently writes results later.
    """
    if not (MIN_TIMEOUT_SECONDS <= limits.timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise V3ContractError(
            f"timeout {limits.timeout_seconds} is outside the permitted "
            f"[{MIN_TIMEOUT_SECONDS}, {MAX_TIMEOUT_SECONDS}] seconds"
        )
    return limits


def propagate_cancellation(
    *, node_id: str, cancelled: bool, timed_out: bool
) -> V3MappedError | None:
    """Return the cancellation to record, or ``None`` if the run finished.

    Both an explicit cancel and a deadline expiry produce the same mapped
    outcome (workflow ``cancelled``), because from the workflow's side the
    node stopped for a reason outside the run and its partial output is not a
    result. ``node_id`` is always populated.
    """
    if not cancelled and not timed_out:
        return None
    reason = V3ErrorCode.DEADLINE_EXCEEDED if timed_out else V3ErrorCode.CANCELLED
    detail = V3ErrorDetail(code=reason, message="dispatch stopped before completion")
    return map_error(detail, node_id=node_id)


# --------------------------------------------------------------------------- #
# Declared-vs-live agreement
# --------------------------------------------------------------------------- #

#: The V3 module this contract mirrors, by import name. Used by tests to prove
#: the contract's vocabulary still matches V3, and by the contract itself to
#: prove it never imports V3 at runtime.
_V3_MODULE = "agents.interfaces"


def assert_no_v3_import(module_globals: dict[str, object]) -> None:
    """Fail if a V3 module is reachable from the caller's globals.

    Callers hand in ``globals()``; the check is a name scan, not an import,
    so the workflow engine stays free of a runtime dependency on ``packages.agents``.
    """
    module_name = str(module_globals.get("__name__", ""))
    if module_name.split(".")[0] == "agents":
        raise V3ContractError("the V3 seam contract must not live inside V3")
    for key, value in module_globals.items():
        if key.startswith("__"):
            continue
        module_of = getattr(value, "__module__", None)
        if isinstance(module_of, str) and module_of.split(".")[0] == "agents":
            raise V3ContractError(
                f"{key} resolves into the V3 package; the seam must not import V3"
            )


__all__ = [
    "MAX_CAPABILITIES",
    "MAX_ERROR_MESSAGE_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "V3CapabilityMapping",
    "V3ContractError",
    "V3DispatchLimits",
    "V3ErrorCode",
    "V3ErrorDetail",
    "V3MappedError",
    "V3NodeRequest",
    "V3NodeResponse",
    "V3RunStatus",
    "WorkflowOutcome",
    "assert_capability_allowed",
    "assert_no_v3_import",
    "map_error",
    "map_manifest",
    "map_response",
    "propagate_cancellation",
    "validate_dispatch",
]
