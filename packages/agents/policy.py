"""Workspace Agent Policy & Limit Derivation (T-R14).

Server-side policy governing allowed models, tools, and budget caps per workspace.
Enforces invariant:
    Requests may NARROW limits, NEVER widen org policy.
    Above-policy requests are strictly refused (PolicyViolationError).
    Below-policy requests are honored.
"""

from __future__ import annotations

import threading
from typing import Any

from pydantic import BaseModel, ConfigDict

DEFAULT_ALLOWED_MODELS = (
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-26b-a4b-it",
)
DEFAULT_MODEL = "google/gemma-4-26b-a4b-it:free"

DEFAULT_ALLOWED_TOOLS = (
    "calculator",
    "knowledge_search",
    "knowledge_read",
    "publish",
)


class PolicyViolationError(ValueError):
    """Raised when an agent run request attempts to widen workspace policy limits."""

    def __init__(
        self, field: str, requested: Any, limit: Any, message: str | None = None
    ) -> None:
        msg = (
            message
            or f"Requested {field}={requested!r} exceeds server-side policy limit {limit!r}"
        )
        super().__init__(msg)
        self.field = field
        self.requested = requested
        self.limit = limit


class WorkspaceAgentPolicy(BaseModel):
    """Server-side policy for a workspace governing agent execution."""

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    allowed_models: tuple[str, ...] = DEFAULT_ALLOWED_MODELS
    default_model: str = DEFAULT_MODEL
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    max_steps: int = 10
    max_tokens: int = 100_000
    max_tool_calls: int = 50
    max_wall_clock_s: float = 300.0
    max_cost_usd: float = 1.0  # $1.00 max spend per run
    max_tool_cost_usd: float = 0.50  # $0.50 max tool spend per run


class EffectiveRunConfig(BaseModel):
    """Effective configuration derived after applying server policy and request narrowing."""

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    model: str
    allowed_models: list[str]
    allowed_tools: list[str]
    max_steps: int
    max_tokens: int
    max_tool_calls: int
    max_wall_clock_s: float
    max_cost_usd: float
    max_tool_cost_usd: float


def derive_effective_run_config(
    policy: WorkspaceAgentPolicy,
    *,
    goal: str,
    requested_model: str | None = None,
    requested_tools: list[str] | None = None,
    requested_steps: int | None = None,
    requested_tokens: int | None = None,
    requested_tool_calls: int | None = None,
    requested_cost_usd: float | None = None,
    requested_tool_cost_usd: float | None = None,
    requested_wall_clock_s: float | None = None,
) -> EffectiveRunConfig:
    """Derive effective agent run configuration by combining server policy and request.

    Enforces:
    - Requests may NARROW limits (lower steps, fewer tools, lower cost cap, etc.)
    - Requests may NEVER WIDEN org policy (higher steps, unallowed tools, unallowed model)
    - Above-policy requests raise PolicyViolationError immediately.
    """
    # 1. Model validation & derivation
    if requested_model is not None and requested_model.strip():
        req_m = requested_model.strip()
        if req_m not in policy.allowed_models:
            raise PolicyViolationError(
                field="model",
                requested=req_m,
                limit=list(policy.allowed_models),
                message=(
                    f"Requested model {req_m!r} is not allowed by "
                    f"workspace policy {list(policy.allowed_models)!r}"
                ),
            )
        effective_model = req_m
    else:
        effective_model = policy.default_model

    # 2. Tool allowlist derivation
    if requested_tools is not None and len(requested_tools) > 0:
        disallowed = [t for t in requested_tools if t not in policy.allowed_tools]
        if disallowed:
            raise PolicyViolationError(
                field="allowed_tools",
                requested=disallowed,
                limit=list(policy.allowed_tools),
                message=(
                    f"Requested tools {disallowed!r} are not allowed by "
                    f"workspace policy {list(policy.allowed_tools)!r}"
                ),
            )
        # Narrowed tools: only the requested subset
        effective_tools = [t for t in policy.allowed_tools if t in requested_tools]
    else:
        effective_tools = list(policy.allowed_tools)

    # 3. Step budget derivation (narrowing allowed, widening refused)
    if requested_steps is not None:
        if requested_steps > policy.max_steps:
            raise PolicyViolationError(
                field="max_steps",
                requested=requested_steps,
                limit=policy.max_steps,
                message=(
                    f"Requested max_steps ({requested_steps}) exceeds "
                    f"workspace policy cap ({policy.max_steps})"
                ),
            )
        effective_steps = requested_steps
    else:
        effective_steps = policy.max_steps

    # 4. Token budget derivation
    if requested_tokens is not None:
        if requested_tokens > policy.max_tokens:
            raise PolicyViolationError(
                field="max_tokens",
                requested=requested_tokens,
                limit=policy.max_tokens,
                message=(
                    f"Requested max_tokens ({requested_tokens}) exceeds "
                    f"workspace policy cap ({policy.max_tokens})"
                ),
            )
        effective_tokens = requested_tokens
    else:
        effective_tokens = policy.max_tokens

    # 5. Tool call budget derivation
    if requested_tool_calls is not None:
        if requested_tool_calls > policy.max_tool_calls:
            raise PolicyViolationError(
                field="max_tool_calls",
                requested=requested_tool_calls,
                limit=policy.max_tool_calls,
                message=(
                    f"Requested max_tool_calls ({requested_tool_calls}) exceeds "
                    f"workspace policy cap ({policy.max_tool_calls})"
                ),
            )
        effective_tool_calls = requested_tool_calls
    else:
        effective_tool_calls = policy.max_tool_calls

    # 6. Cost USD cap derivation
    if requested_cost_usd is not None:
        if requested_cost_usd > policy.max_cost_usd:
            raise PolicyViolationError(
                field="max_cost_usd",
                requested=requested_cost_usd,
                limit=policy.max_cost_usd,
                message=(
                    f"Requested max_cost_usd (${requested_cost_usd:.4f}) exceeds "
                    f"workspace policy cap (${policy.max_cost_usd:.4f})"
                ),
            )
        effective_cost_usd = requested_cost_usd
    else:
        effective_cost_usd = policy.max_cost_usd

    # 7. Tool cost USD cap derivation
    if requested_tool_cost_usd is not None:
        if requested_tool_cost_usd > policy.max_tool_cost_usd:
            raise PolicyViolationError(
                field="max_tool_cost_usd",
                requested=requested_tool_cost_usd,
                limit=policy.max_tool_cost_usd,
                message=(
                    f"Requested max_tool_cost_usd (${requested_tool_cost_usd:.4f}) exceeds "
                    f"workspace policy cap (${policy.max_tool_cost_usd:.4f})"
                ),
            )
        effective_tool_cost_usd = requested_tool_cost_usd
    else:
        effective_tool_cost_usd = policy.max_tool_cost_usd

    # 8. Wall clock seconds derivation
    if requested_wall_clock_s is not None:
        if requested_wall_clock_s > policy.max_wall_clock_s:
            raise PolicyViolationError(
                field="max_wall_clock_s",
                requested=requested_wall_clock_s,
                limit=policy.max_wall_clock_s,
                message=(
                    f"Requested max_wall_clock_s ({requested_wall_clock_s:.1f}s) exceeds "
                    f"workspace policy cap ({policy.max_wall_clock_s:.1f}s)"
                ),
            )
        effective_wall_clock_s = requested_wall_clock_s
    else:
        effective_wall_clock_s = policy.max_wall_clock_s

    return EffectiveRunConfig(
        workspace_id=policy.workspace_id,
        model=effective_model,
        allowed_models=list(policy.allowed_models),
        allowed_tools=effective_tools,
        max_steps=effective_steps,
        max_tokens=effective_tokens,
        max_tool_calls=effective_tool_calls,
        max_wall_clock_s=effective_wall_clock_s,
        max_cost_usd=effective_cost_usd,
        max_tool_cost_usd=effective_tool_cost_usd,
    )


class WorkspacePolicyStore:
    """Thread-safe store for workspace-specific agent policies."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._policies: dict[str, WorkspaceAgentPolicy] = {}

    def get_policy(self, workspace_id: str) -> WorkspaceAgentPolicy:
        with self._lock:
            policy = self._policies.get(workspace_id)
            if policy is not None:
                return policy
            return WorkspaceAgentPolicy(workspace_id=workspace_id)

    def set_policy(self, workspace_id: str, policy: WorkspaceAgentPolicy) -> None:
        with self._lock:
            self._policies[workspace_id] = policy

    def delete_policy(self, workspace_id: str) -> None:
        with self._lock:
            self._policies.pop(workspace_id, None)

    def reset(self) -> None:
        with self._lock:
            self._policies.clear()


_GLOBAL_POLICY_STORE = WorkspacePolicyStore()


def get_workspace_policy(workspace_id: str) -> WorkspaceAgentPolicy:
    """Get the active policy for a workspace."""
    return _GLOBAL_POLICY_STORE.get_policy(workspace_id)


def set_workspace_policy(workspace_id: str, policy: WorkspaceAgentPolicy) -> None:
    """Set a workspace-specific policy."""
    _GLOBAL_POLICY_STORE.set_policy(workspace_id, policy)


def reset_workspace_policies() -> None:
    """Reset all registered workspace policies to defaults."""
    _GLOBAL_POLICY_STORE.reset()
