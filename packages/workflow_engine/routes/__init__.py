"""Subpackage for HTTP route contracts of the workflow engine (T-V4-019 / W4).

Schemas only — no routers, no handlers, no store calls. See
:mod:`workflow_engine.routes.schemas`.
"""

from __future__ import annotations

from .schemas import ROUTE_CONTRACTS, contract_for, schemas_for_route

__all__ = ["ROUTE_CONTRACTS", "contract_for", "schemas_for_route"]
