"""Agent Tool Framework (Sections 26 & 27).

Provides standard tools (calculator, with knowledge-read/search tools arriving in
A1-1) and a thread-safe ToolRegistry.

Python execution is DISABLED in this V3 release (plan invariant 5): no
tool-produced Python runs in the API or shared worker process. The earlier
in-process sandbox was removed in A0-1 review B1 — its `exec()` with a deny-list
guard was trivially bypassable. If code execution is ever approved it belongs in
a separately isolated worker (nsjail/firejail/gVisor + ulimit + wall-clock
timeout + read-only FS + no network + no production credentials), behind its own
reviewed capability.
"""

from __future__ import annotations

import ast
import operator
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class Tool(ABC):
    """Abstract tool definition."""

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[dict[str, Any]]

    @abstractmethod
    async def execute(self, workspace_id: str, **kwargs: Any) -> Any:
        """Execute the tool within the given workspace scope."""
        raise NotImplementedError


class CalculatorTool(Tool):
    """Safe arithmetic expression calculator using AST parsing."""

    name: ClassVar[str] = "calculator"
    description: ClassVar[str] = "Compute arithmetic expressions (e.g. '12 * 45 + 10')."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "Mathematical expression to evaluate"}
        },
        "required": ["expression"],
    }

    _OPS: ClassVar[dict[type, Any]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    _UNARY_OPS: ClassVar[dict[type, Any]] = {
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def _eval_node(self, node: ast.AST) -> float | int:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp):
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            op_type = type(node.op)
            if op_type in self._OPS:
                return self._OPS[op_type](left, right)
            raise ValueError(f"Unsupported binary operator: {op_type}")
        if isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand)
            uop_type = type(node.op)
            if uop_type in self._UNARY_OPS:
                return self._UNARY_OPS[uop_type](operand)
            raise ValueError(f"Unsupported unary operator: {uop_type}")
        raise ValueError(f"Unsupported expression element: {type(node).__name__}")

    async def execute(self, workspace_id: str, **kwargs: Any) -> Any:
        expr = kwargs.get("expression", "")
        if not expr:
            return {"error": "Missing expression"}
        try:
            tree = ast.parse(expr, mode="eval")
            result = self._eval_node(tree.body)
            return {"result": result}
        except Exception as exc:
            return {"error": f"Evaluation error: {exc}"}


class ToolRegistry(BaseModel):
    """Registry maintaining available tools."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tools: dict[str, Tool] = Field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self.tools.values())
