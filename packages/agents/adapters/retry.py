"""RetryPolicy/TimeoutPolicy ↔ is_retryable/bounded_backoff — skeleton (T-V3-011).

Both our `RetryPolicy(max_attempts, initial_backoff_s, backoff_multiplier,
max_backoff_s, retryable_error_codes)` + `compute_backoff`/`bounded_backoff`
(capped at `max_backoff` and `deadline`) and LangGraph's `RetryPolicy`
(max_attempts, initial_interval, backoff_factor, max_interval, jitter) +
`TimeoutPolicy` (per-node) are the same shape. This adapter maps one to the
other and keeps `is_retryable`/`bounded_backoff` as the single authorities.

No `import langgraph` — the adapter is the seam.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..interfaces import AgentErrorCode, RetryPolicy


class RetryAdapter:
    """Adapter that bridges product `RetryPolicy` ↔ LangGraph `RetryPolicy`."""

    @staticmethod
    def is_retryable(error_code: AgentErrorCode | None, policy: RetryPolicy) -> bool:
        from ..runtime import is_retryable

        return is_retryable(error_code, policy)

    @staticmethod
    def compute_backoff(attempt: int, policy: RetryPolicy) -> float:
        from ..runtime import compute_backoff

        return compute_backoff(attempt, policy)

    @staticmethod
    def bounded_backoff(attempt: int, policy: RetryPolicy, deadline: datetime) -> float:
        from ..runtime import bounded_backoff

        return bounded_backoff(attempt, policy, deadline)

    @staticmethod
    def to_langgraph_retry(policy: RetryPolicy) -> dict[str, Any]:
        """Translate product `RetryPolicy` to LangGraph `RetryPolicy` shape (stub)."""
        return {
            "max_attempts": policy.max_attempts,
            "initial_interval": policy.initial_backoff_s,
            "backoff_factor": policy.backoff_multiplier,
            "max_interval": policy.max_backoff_s,
            "retry_on": list(policy.retryable_error_codes),
        }

    @staticmethod
    def to_langgraph_timeout(timeout_s: float) -> dict[str, Any]:
        """Translate product `ToolSpec.timeout_s` to LangGraph `TimeoutPolicy` shape (stub)."""
        return {"timeout": timeout_s}

    @staticmethod
    def from_langgraph_retry(data: dict[str, Any]) -> RetryPolicy:
        """Translate LangGraph `RetryPolicy` shape back to product `RetryPolicy` (stub)."""
        return RetryPolicy(
            max_attempts=int(data.get("max_attempts", 1)),
            initial_backoff_s=float(data.get("initial_interval", 0.0)),
            backoff_multiplier=float(data.get("backoff_factor", 2.0)),
            max_backoff_s=float(data.get("max_interval", 30.0)),
            retryable_error_codes=list(data.get("retry_on", [])),
        )
