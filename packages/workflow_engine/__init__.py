"""Workflow Engine package (Phase 13 / Section 30).

W0/W1 exports: strict contracts, validator, compiler, and fake runtime.
The prototype `WorkflowEngine` is preserved for backward compatibility.
"""

from .compiler import WorkflowCompiler
from .engine import WorkflowEngine
from .executor import FakeNodeExecutor, WorkflowExecutor
from .interfaces import (
    # W0/W1 strict contracts
    CapabilityManifest,
    ExecutionEdgeInfo,
    ExecutionPlan,
    ExecutionStep,
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
from .validator import WorkflowValidator

__all__ = [
    # W0/W1 contracts
    "CapabilityManifest",
    "ExecutionEdgeInfo",
    "ExecutionPlan",
    "ExecutionStep",
    "FakeNodeExecutor",
    "NodeExecutor",
    # Prototype (preserved entrypoint)
    "NodeRun",
    "NodeRunStatus",
    "NodeSpec",
    "NodeType",
    "PortSpec",
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
]
