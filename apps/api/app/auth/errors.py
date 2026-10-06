"""Auth-safe HTTP error handling.

FastAPI's default ``RequestValidationError`` body echoes the submitted input
(including passwords). The handler registered here strips every field's
``input``/``ctx`` so no credential can ever appear in an error response or
request log. Messages stay generic to avoid user enumeration.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def _sanitize_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for error in errors:
        sanitized.append(
            {key: value for key, value in error.items() if key not in {"input", "ctx", "url"}}
        )
    return sanitized


def register_auth_exception_handlers(app: FastAPI) -> None:
    """Install the redacting validation handler on the application."""

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": _sanitize_errors(exc.errors()),
                "error": "validation_error",
                "message": "Invalid request payload.",
            },
        )
