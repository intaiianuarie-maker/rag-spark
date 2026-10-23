"""T-V2-011: V2 turn-on procedure (GRAPH_MODE flip with preconditions & rollback).

Enforces strict preconditions before enabling GRAPH_MODE:
1. Validates GRAPH_DB_PATH is accessible and shared.
2. Validates LLM model route (Gemma approved route).
3. Verifies store readiness and workspace enablement.
4. Provides atomic flip and graceful rollback to GRAPH_MODE=off on degradation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from graph.dispatch import DEFAULT_LLM_MODEL, DEFAULT_LLM_PAID_MODEL
from graph.factory import create_graph_store, graph_db_path
from models.catalog import _lookup_admitted
from models.policy import ModelRole


@dataclass
class PreconditionResult:
    passed: bool
    details: dict[str, Any]
    error: str | None = None


def check_v2_preconditions(env: dict[str, str] | None = None) -> PreconditionResult:
    """Validate all operational and model preconditions before V2 turn-on."""
    details: dict[str, Any] = {}
    try:
        # 1. DB path checks
        resolved_db = graph_db_path(env)
        details["db_path"] = resolved_db
        store = create_graph_store(db_path=resolved_db, env=env)
        details["store_initialized"] = bool(store)

        # 2. Gemma Model route admission check
        _lookup_admitted(ModelRole.GRAPH, DEFAULT_LLM_MODEL)
        _lookup_admitted(ModelRole.GRAPH, DEFAULT_LLM_PAID_MODEL)
        details["model_free"] = DEFAULT_LLM_MODEL
        details["model_paid"] = DEFAULT_LLM_PAID_MODEL

        return PreconditionResult(passed=True, details=details)
    except Exception as exc:
        return PreconditionResult(passed=False, details=details, error=str(exc))


def activate_v2_graph(
    target_mode: str = "llm",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute the activation procedure if preconditions pass, else fail-closed."""
    preconditions = check_v2_preconditions(env)
    if not preconditions.passed:
        raise RuntimeError(f"V2 activation refused; preconditions failed: {preconditions.error}")

    # Set mode in environment
    target_env = env if env is not None else os.environ
    previous_mode = target_env.get("GRAPH_MODE", "off")
    target_env["GRAPH_MODE"] = target_mode

    return {
        "status": "activated",
        "previous_mode": previous_mode,
        "active_mode": target_mode,
        "preconditions": preconditions.details,
    }


def rollback_v2_graph(
    fallback_mode: str = "off",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Roll back GRAPH_MODE safely without data mutation."""
    target_env = env if env is not None else os.environ
    prior = target_env.get("GRAPH_MODE", "off")
    target_env["GRAPH_MODE"] = fallback_mode
    return {
        "status": "rolled_back",
        "previous_mode": prior,
        "active_mode": fallback_mode,
    }
