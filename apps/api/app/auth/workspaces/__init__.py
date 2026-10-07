"""Organizations, workspaces and memberships (TASK-M1-001b).

Vendor-free: SQLAlchemy + Pydantic + core auth types only. Authorization is
centralized in :mod:`app.auth.workspaces.policy` (ticket M1-001c replaces it
with the product-wide ``authz.can()`` service).
"""

from __future__ import annotations

from app.auth.workspaces.router import router

__all__ = ["router"]
