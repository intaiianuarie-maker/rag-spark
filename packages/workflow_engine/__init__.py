"""Workflow Engine package (Phase 13 / Section 30).

W0/W1 exports: strict contracts, validator, compiler, and fake runtime.
W2 exports: restricted Langflow import adapter (compatibility spike).
The prototype `WorkflowEngine` is preserved for backward compatibility.
"""

from .adapters import (
    LangflowAdapter,
    LangflowComponentSpec,
    LangflowImportError,
)
from .compiler import WorkflowCompiler
from .engine import WorkflowEngine
from .executor import FakeNodeExecutor, WorkflowExecutor
from .interfaces import (
    # W0/W1 strict contracts
    CapabilityManifest,
    ExecutionEdgeInfo,
    ExecutionPlan,
    ExecutionStep,
    JoinStrategy,
    NodeExecutor,
    # Prototype models (preserved)
    NodeRun,
    NodeRunStatus,
    NodeSpec,
    NodeType,
    PortSpec,
    ValidationError,
    ValidationResult,
    Workflow,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowEdgeSpec,
    WorkflowNode,
    WorkflowRevision,
    WorkflowRun,
    WorkflowRunStatus,
)
from .manifest import (
    PRODUCTION_CAPABILITY_MANIFEST,
    CapabilityNotSupportedError,
    get_production_capability_manifest,
)
from .production_executor import ProductionNodeExecutor
from .validator import WorkflowValidator

__all__ = [
    "PRODUCTION_CAPABILITY_MANIFEST",
    "CapabilityManifest",
    "CapabilityNotSupportedError",
    "ExecutionEdgeInfo",
    "ExecutionPlan",
    "ExecutionStep",
    "FakeNodeExecutor",
    "JoinStrategy",
    "LangflowAdapter",
    "LangflowComponentSpec",
    "LangflowImportError",
    "NodeExecutor",
    "NodeRun",
    "NodeRunStatus",
    "NodeSpec",
    "NodeType",
    "PortSpec",
    "ProductionNodeExecutor",
    "ValidationError",
    "ValidationResult",
    "Workflow",
    "WorkflowCompiler",
    "WorkflowDefinition",
    "WorkflowEdge",
    "WorkflowEdgeSpec",
    "WorkflowEngine",
    "WorkflowExecutor",
    "WorkflowNode",
    "WorkflowRevision",
    "WorkflowRun",
    "WorkflowRunStatus",
    "WorkflowValidator",
    "get_production_capability_manifest",
]

