"""Workflow engine adapters (W2+).

Adapter boundaries keep third-party authoring/runtime formats out of the
product contracts.  The Langflow adapter is a pure translator for a restricted
declarative subset — it never installs, imports or executes Langflow or any
custom component code.
"""

from .langflow_adapter import (
    LangflowAdapter,
    LangflowComponentSpec,
    LangflowImportError,
)

__all__ = [
    "LangflowAdapter",
    "LangflowComponentSpec",
    "LangflowImportError",
]
