"""Production capability manifest and capability gating (T-R20).

Enforces capability bounds at both compile-time and runtime. Unsupported
capabilities or unconfigured node types refuse loudly rather than fake-succeeding.
"""

from __future__ import annotations

from .interfaces import CapabilityManifest, NodeType


class CapabilityNotSupportedError(NotImplementedError):
    """Raised when a workflow node requires an unsupported or disabled capability."""


#: Production capability manifest: explicit node types and allowed capabilities.
#: Unsupported capabilities (e.g. unconfigured tools, arbitrary code execution, raw http)
#: are excluded so attempts to execute them refuse loudly rather than fake-succeed.
PRODUCTION_CAPABILITY_MANIFEST = CapabilityManifest(
    supported_node_types={
        NodeType.TRIGGER,
        NodeType.TRANSFORM,
        NodeType.CONDITION,
        NodeType.OUTPUT,
        NodeType.LLM,
        NodeType.AGENT,
    },
    enabled_capabilities={
        "llm",
        "transform",
        "condition",
        "agent",
        "calculator",
        "knowledge_search",
        "knowledge_read",
    },
)


def get_production_capability_manifest() -> CapabilityManifest:
    """Return the default production capability manifest."""
    return PRODUCTION_CAPABILITY_MANIFEST
