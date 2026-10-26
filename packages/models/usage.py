"""Call-scoped stream metadata without changing the text iterator contract.

Observers bind a fresh collector only while advancing/closing their iterator.
Providers retain that collector locally; neither provider instances nor callers
inherit a mutable global "last usage" value.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass
class StreamUsage:
    model: str | None = None
    model_status: str = "not_reported"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None

    @property
    def usage_status(self) -> str:
        return (
            "reported" if self.prompt_tokens is not None and self.completion_tokens is not None
            else "not_reported"
        )

    def observe(self, frame: dict[str, Any]) -> None:
        model = frame.get("model")
        if isinstance(model, str) and model:
            self.model = model
            self.model_status = "reported"
        usage = frame.get("usage")
        if not isinstance(usage, dict):
            return
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setattr(self, name, value)
        cost = usage.get("cost")
        if (isinstance(cost, (int, float)) and not isinstance(cost, bool)
                and math.isfinite(cost) and cost >= 0):
            self.cost_usd = float(cost)


stream_usage_ctx: ContextVar[StreamUsage | None] = ContextVar("stream_usage", default=None)


@contextmanager
def capture_stream_usage(collector: StreamUsage):
    token = stream_usage_ctx.set(collector)
    try:
        yield collector
    finally:
        stream_usage_ctx.reset(token)
