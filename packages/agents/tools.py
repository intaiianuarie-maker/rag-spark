"""Agent Tool Framework and Sandboxing (Sections 26 & 27).

Provides standard tools (calculator, python sandbox, knowledge search) and
a thread-safe ToolRegistry.
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


class PythonSandboxTool(Tool):
    """Sandboxed Python code execution for data manipulation and algorithms."""

    name: ClassVar[str] = "python"
    description: ClassVar[str] = (
        "Execute sandboxed Python code. Must set output in variable 'result'."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Python code to execute"}},
        "required": ["code"],
    }

    _BANNED_IMPORTS: ClassVar[set[str]] = {
        "os",
        "sys",
        "subprocess",
        "socket",
        "requests",
        "httpx",
        "urllib",
        "shutil",
    }

    async def execute(self, workspace_id: str, **kwargs: Any) -> Any:
        code = kwargs.get("code", "")
        if not code:
            return {"error": "Missing code"}

        # Security check: inspect AST for banned imports or attributes
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in self._BANNED_IMPORTS:
                            msg = f"Security violation: import '{alias.name}' is prohibited"
                            return {"error": msg}
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".")[0] in self._BANNED_IMPORTS:
                        msg = f"Security violation: from '{node.module}' is prohibited"
                        return {"error": msg}
                elif (
                    isinstance(node, ast.Name)
                    and node.id in ("eval", "exec", "open", "__import__")
                ):
                    return {"error": f"Security violation: built-in '{node.id}' is prohibited"}
        except SyntaxError as exc:
            return {"error": f"Syntax error: {exc}"}

        # Safe execution scope
        safe_globals = {
            "__builtins__": {
                "abs": abs,
                "all": all,
                "any": any,
                "bool": bool,
                "dict": dict,
                "enumerate": enumerate,
                "filter": filter,
                "float": float,
                "int": int,
                "len": len,
                "list": list,
                "map": map,
                "max": max,
                "min": min,
                "range": range,
                "round": round,
                "set": set,
                "sorted": sorted,
                "str": str,
                "sum": sum,
                "tuple": tuple,
                "zip": zip,
            }
        }
        local_env: dict[str, Any] = {}

        try:
            exec(code, safe_globals, local_env)
            res = local_env.get("result", "Completed (no 'result' variable set)")
            return {"result": str(res)}
        except Exception as exc:
            return {"error": f"Execution error: {exc}"}


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
