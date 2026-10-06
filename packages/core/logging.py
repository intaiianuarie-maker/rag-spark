"""Structured logging for API and worker.

Reused: the standard library `logging` + `json`.
Custom: a tiny JSON formatter and a request-id context variable so every log
line carries a correlation id. No logging framework is introduced.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes set by the logging machinery; not user "extra" fields.
_STANDARD_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRS and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_ctx.get()
        if request_id:
            payload["request_id"] = request_id
        payload.update(_extras(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable fallback for local development."""

    def format(self, record: logging.LogRecord) -> str:
        request_id = request_id_ctx.get()
        line = f"{self.formatTime(record)} {record.levelname:<7} {record.name}"
        if request_id:
            line += f" [{request_id[:8]}]"
        line += f" {record.getMessage()}"
        extras = _extras(record)
        if extras:
            line += " " + json.dumps(extras, default=str, ensure_ascii=False)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configure the root logger once, replacing existing handlers."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; route them through ours for JSON logs.
    for uvicorn_logger in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(uvicorn_logger)
        logger.handlers[:] = [handler]
        logger.propagate = False


def new_request_id() -> str:
    """Generate a new correlation id."""
    from uuid import uuid4

    return uuid4().hex


def set_request_id(request_id: str | None) -> Token[str | None]:
    return request_id_ctx.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    request_id_ctx.reset(token)
