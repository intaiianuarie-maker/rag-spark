"""Authorization package (M1-001c): the single ``can()`` decision service.

Everything that decides "may this caller do this action on this resource" lives
here. Route handlers and services call :func:`can`/:func:`require`; no other
module compares roles (enforced by the CI authorization guard). This replaces
the interim ``app.auth.workspaces.policy`` module.
"""

from __future__ import annotations

from app.authz.actions import Action
from app.authz.errors import ApiError, AuthorizationError
from app.authz.policy import (
    ROLE_ACTIONS,
    Resource,
    can,
    ensure_last_owner_safe,
    ensure_owner_can_manage_owner,
    require,
)

__all__ = [
    "ROLE_ACTIONS",
    "Action",
    "ApiError",
    "AuthorizationError",
    "Resource",
    "can",
    "ensure_last_owner_safe",
    "ensure_owner_can_manage_owner",
    "require",
]
