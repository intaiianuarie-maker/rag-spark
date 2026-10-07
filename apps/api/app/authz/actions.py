"""Authorization actions (M1-001c).

`workspace.manage` and `document.read` are required by the ticket; the rest are
the minimal set the M1 surface needs (`workspace.read` for listing/getting,
`document.write` for M2 ingestion, `key.manage` for API keys).
"""

from __future__ import annotations

from enum import StrEnum


class Action(StrEnum):
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_MANAGE = "workspace.manage"
    DOCUMENT_READ = "document.read"
    DOCUMENT_WRITE = "document.write"
    KEY_MANAGE = "key.manage"
