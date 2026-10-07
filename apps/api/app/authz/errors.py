"""Typed errors raised by the authorization service and mapped to HTTP."""

from __future__ import annotations


class ApiError(Exception):
    """An error the API layer maps to ``{"detail": {"error", "message"}}``."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class AuthorizationError(ApiError):
    """Caller is authenticated but not permitted (403)."""

    def __init__(
        self,
        message: str = "You do not have permission to perform this action.",
        code: str = "forbidden",
    ) -> None:
        super().__init__(403, code, message)
