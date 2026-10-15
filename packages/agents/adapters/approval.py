"""Interrupt/Command ↔ WAITING_APPROVAL — skeleton (T-V3-011).

Our `WAITING_APPROVAL` is scoped to (run_id, principal_id, tool_name,
policy_revision) and re-checks on resume (stale rev1 → rev2 stays WAITING,
cross-run and cross-principal replay both fail, same bug class as T-050).
LangGraph's `interrupt(value)` + `Command(resume=...)` is generic human-in-the-loop.
This adapter makes `interrupt` carry the product's scoped waiting tuple and
`Command` resume re-check via `is_approved`.

No `import langgraph` — the adapter is the seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..interfaces import AgentErrorCode

# --- T-A14 / V3-03: server-side approval resolution --------------------------
# The approve route used to re-queue the run and nothing else, so the runtime
# re-check ``_is_approved(run, principal, tool, policy_revision)`` never became
# true and a restored run looped WAITING_APPROVAL -> QUEUED -> WAITING forever.
# These types make the refusal reasons explicit instead of silent.


class ApprovalBindingError(RuntimeError):
    """Base: an approval could not be bound to a real pending action."""

    code = "approval_binding_failed"


class ApprovalTargetMissing(ApprovalBindingError):
    """No pending action is recorded for this run — nothing to approve."""

    code = "approval_target_missing"


class ApprovalAuthorityDenied(ApprovalBindingError):
    """The approver is not the principal the pending action belongs to."""

    code = "approval_authority_denied"


class ApprovalPolicyStale(ApprovalBindingError):
    """The pending action was raised under a different policy revision."""

    code = "approval_policy_stale"


class ApprovalActionMismatch(ApprovalBindingError):
    """The requested tool is not the action actually awaiting approval."""

    code = "approval_action_mismatch"


@dataclass(frozen=True)
class ResolvedApproval:
    """The durable binding written by :meth:`ApprovalAdapter.resolve_and_approve`."""

    run_id: str
    principal_id: str
    tool_name: str
    policy_revision: str

# Reuse the product's stores — the adapter never duplicates the check.
# Imported lazily inside methods to avoid cycles, but the delegation is
# to `runtime._is_approved` / `runtime._is_write_tool` / `runtime.approve_run`
# which are the single authorities.


class ApprovalAdapter:
    """Adapter that bridges `interrupt`/`Command` ↔ `WAITING_APPROVAL`."""

    @staticmethod
    def is_write_tool(tool_name: str) -> bool:
        from ..runtime import _is_write_tool

        return _is_write_tool(tool_name)

    @staticmethod
    def is_approved(run_id: str, principal_id: str, tool_name: str, policy_revision: str) -> bool:
        from ..runtime import _is_approved

        return _is_approved(run_id, principal_id, tool_name, policy_revision)

    @staticmethod
    def approve(run_id: str, principal_id: str, tool_name: str, policy_revision: str) -> None:
        from ..runtime import approve_run

        approve_run(run_id, principal_id, tool_name, policy_revision)

    @staticmethod
    def resolve_and_approve(
        run_id: str,
        *,
        approver_principal_id: str,
        run_principal_id: str,
        current_policy_revision: str | None,
        tool_name: str | None = None,
    ) -> ResolvedApproval | None:
        """Resolve the pending action server-side and bind the approval (T-A14).

        The pending tuple is ``(tool, raw_input, policy_revision, principal)`` —
        the authority lives in :mod:`agents.runtime`, this method never invents
        an action. Every refusal is a typed error, never a silent re-queue:

        * ``ApprovalTargetMissing`` — no pending action for this run.
        * ``ApprovalAuthorityDenied`` — approver is not the run's principal.
        * ``ApprovalPolicyStale`` — the action was raised under another policy
          revision (approval bound to the waiting revision would not match on
          resume, so the run would stay blocked forever).
        * ``ApprovalActionMismatch`` — the requested tool is not the waiting one.

        Returns the binding, or ``None`` when no pending action is in this
        process (e.g. an API replica that never ran the worker) — the caller
        then keeps the legacy re-queue instead of writing a phantom approval.
        """
        from ..runtime import _WAITING, approve_run

        waiting = _WAITING.get(run_id)
        if waiting is None:
            # No pending action visible here: do NOT write a phantom approval.
            return None

        w_tool, _w_input, w_revision, w_principal = waiting

        # Authority: only the principal the action belongs to may approve it.
        if approver_principal_id != run_principal_id:
            raise ApprovalAuthorityDenied(
                "approver is not the principal that owns this run"
            )
        if w_principal != run_principal_id:
            raise ApprovalAuthorityDenied(
                "pending action belongs to a different principal than the run"
            )
        # Policy: the binding must match the revision the action was raised at,
        # otherwise the runtime re-check on resume can never pass.
        if current_policy_revision is not None and w_revision != current_policy_revision:
            raise ApprovalPolicyStale(
                f"pending action was raised at policy revision {w_revision!r}, "
                f"run is now at {current_policy_revision!r}"
            )
        # Action: an approval names one exact pending action.
        if tool_name is not None and tool_name != w_tool:
            raise ApprovalActionMismatch(
                f"cannot approve {tool_name!r}: the pending action is {w_tool!r}"
            )

        approve_run(run_id, w_principal, w_tool, w_revision)
        return ResolvedApproval(
            run_id=run_id,
            principal_id=w_principal,
            tool_name=w_tool,
            policy_revision=w_revision,
        )

    @staticmethod
    def build_interrupt_payload(
        run_id: str, tool_name: str, raw_input: str, policy_revision: str, principal_id: str
    ) -> dict[str, Any]:
        """Payload that LangGraph's `interrupt()` would carry — product-typed."""
        return {
            "waiting_tool": tool_name,
            "raw_input": raw_input,
            "run_id": run_id,
            "policy_revision": policy_revision,
            "principal_id": principal_id,
            "error": f"write tool {tool_name!r} requires approval",
            "error_code": AgentErrorCode.TOOL_NOT_ALLOWED.value,
        }

    @staticmethod
    def check_resume_allowed(
        run_id: str, principal_id: str, tool_name: str, policy_revision: str
    ) -> bool:
        """Re-check on resume — stale approval must re-block."""
        from ..runtime import _is_approved

        return _is_approved(run_id, principal_id, tool_name, policy_revision)

    # The adapter's `interrupt`/`Command` shape is intentionally not importing
    # `langgraph.types.interrupt` / `Command` — the spike settled for
    # wheel-inspection, and the skeleton must not require the real package
    # to import (BLOCKED otherwise). The types above are product-typed dicts
    # that a future `langgraph` node would translate to `interrupt()` /
    # `Command(resume=...)`.

    @staticmethod
    def to_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        """Translate product waiting payload to LangGraph interrupt value (stub)."""
        return {"type": "interrupt", "value": payload}

    @staticmethod
    def from_command(command: dict[str, Any]) -> dict[str, Any]:
        """Translate LangGraph Command resume to product approval check (stub)."""
        # `command` would be `Command(resume={...})` in real LangGraph
        resume = command.get("resume") or command
        return {
            "run_id": resume.get("run_id"),
            "principal_id": resume.get("principal_id"),
            "tool_name": resume.get("tool_name"),
            "policy_revision": resume.get("policy_revision"),
        }
