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

from typing import Any

from ..interfaces import AgentErrorCode

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
