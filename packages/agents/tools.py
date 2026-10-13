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
import math
import operator
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .interfaces import AgentErrorCode

#: T-V3-001 (V3 plan trace A1-2): an agent's calculator is a CPU sink whose
#: input the **model** chooses, so every dimension of the work is bounded
#: before any of it happens. The bounds are far above real arithmetic and far
#: below anything a hostile input can reach:
#:
#: * :data:`MAX_EXPRESSION_CHARS` bounds ``ast.parse`` itself — parsing is
#:   linear in the source, so an unbounded source is unbounded CPU and memory.
#: * :data:`MAX_AST_NODES` bounds the arithmetic: one node is one operation.
#: * :data:`MAX_AST_DEPTH` bounds the evaluator's recursion and the parser's
#:   nesting. ``1+1+1+…`` nests *linearly*, so a long chain is a deep tree and
#:   depth is what stops it.
#: * :data:`MAX_EXPONENT` bounds ``**``, the only operator whose cost is
#:   super-linear in its operands: ``9**9**9`` is minutes of big-integer work
#:   from seven characters.
#: * :data:`MAX_RESULT_BITS` bounds the integers that may exist at all (4096
#:   bits ≈ 1233 decimal digits), checked on **every** intermediate result.
MAX_EXPRESSION_CHARS = 200
MAX_AST_NODES = 64
MAX_AST_DEPTH = 16
MAX_EXPONENT = 128
MAX_RESULT_BITS = 4096


class CalculatorInputRejected(ValueError):
    """T-V3-001: an input the calculator's declared bounds refuse.

    Typed so that callers and the step trace branch on :attr:`code`, never on
    the message text. Deliberately *returned* rather than raised out of
    ``execute``: the runtime's dispatch maps any exception from a tool to
    ``PROVIDER_ERROR`` (``packages/agents/runtime.py``), which would file a
    refused input as a provider blip. The rejection travels as an output
    payload carrying this same code instead.
    """

    code: ClassVar[AgentErrorCode] = AgentErrorCode.TOOL_SCHEMA_INVALID

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    """Safe arithmetic expression calculator using AST parsing.

    T-V3-001: the evaluator walks a whitelist of node types and arithmetic
    operators only. There is no ``eval``/``exec``/``compile``/``__import__`` on
    this path and nothing here can name, read, call or attribute-hunt anything
    — ``Name``, ``Call`` and ``Attribute`` are rejected as unsupported elements,
    so a payload like ``__import__('os').system('…')`` is refused by the same
    rule that refuses ``[1,2]``. Input is measured (:meth:`_preflight`) before
    any arithmetic runs, so a refused expression costs a length check and, at
    most, one parse of at most :data:`MAX_EXPRESSION_CHARS` characters.
    """

    name: ClassVar[str] = "calculator"
    description: ClassVar[str] = "Compute arithmetic expressions (e.g. '12 * 45 + 10')."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "maxLength": MAX_EXPRESSION_CHARS,
                "description": (
                    "Mathematical expression to evaluate. Arithmetic only "
                    f"(+ - * / // % ** and unary +/-), at most "
                    f"{MAX_EXPRESSION_CHARS} characters, at most "
                    f"{MAX_AST_NODES} operators, nesting at most {MAX_AST_DEPTH} "
                    f"levels, exponents at most {MAX_EXPONENT}."
                ),
            }
        },
        "required": ["expression"],
    }

    MAX_EXPRESSION_CHARS: ClassVar[int] = MAX_EXPRESSION_CHARS
    MAX_AST_NODES: ClassVar[int] = MAX_AST_NODES
    MAX_AST_DEPTH: ClassVar[int] = MAX_AST_DEPTH
    MAX_EXPONENT: ClassVar[int] = MAX_EXPONENT
    MAX_RESULT_BITS: ClassVar[int] = MAX_RESULT_BITS

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

    # --- bounds (T-V3-001) --------------------------------------------------

    def _as_number(self, value: Any) -> int | float:
        """A constant is a finite number and nothing else; returns it narrowed."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CalculatorInputRejected(f"unsupported literal: {value!r}")
        if isinstance(value, float) and not math.isfinite(value):
            raise CalculatorInputRejected("literal is not a finite number")
        return value

    def _check_exponent(self, value: Any) -> None:
        """``**`` is the one super-linear operator, so its exponent is capped."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise CalculatorInputRejected("an exponent must be an integer")
        if abs(value) > self.MAX_EXPONENT:
            raise CalculatorInputRejected(
                f"exponent {value} exceeds the limit of {self.MAX_EXPONENT}"
            )

    def _guard_result(self, value: int | float) -> int | float:
        """Every intermediate result is measured, not just the last one."""
        if isinstance(value, int):
            if value.bit_length() > self.MAX_RESULT_BITS:
                raise CalculatorInputRejected(
                    f"an intermediate integer exceeds {self.MAX_RESULT_BITS} bits"
                )
            return value
        if not math.isfinite(value):
            raise CalculatorInputRejected("a result is not a finite number")
        return value

    def _check_tree(self, node: ast.AST, *, depth: int) -> int:
        """Structural pre-flight. Returns the subtree's node count.

        Nothing is computed here — the whole tree is measured and whitelisted
        first, so an expression that would be expensive never gets the chance.
        """
        if depth > self.MAX_AST_DEPTH:
            raise CalculatorInputRejected(
                f"expression nests deeper than {self.MAX_AST_DEPTH} levels"
            )
        if isinstance(node, ast.Expression):
            return 1 + self._check_tree(node.body, depth=depth + 1)
        if isinstance(node, ast.Constant):
            self._as_number(node.value)
            return 1
        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in self._OPS:
                raise CalculatorInputRejected(
                    f"unsupported operator: {op_type.__name__}"
                )
            count = (
                1
                + self._check_tree(node.left, depth=depth + 1)
                + self._check_tree(node.right, depth=depth + 1)
            )
            # A literal exponent is refused here; a computed one is caught in
            # _eval_node, which is the first place its value is known.
            if op_type is ast.Pow and isinstance(node.right, ast.Constant):
                self._check_exponent(node.right.value)
            return count
        if isinstance(node, ast.UnaryOp):
            uop_type = type(node.op)
            if uop_type not in self._UNARY_OPS:
                raise CalculatorInputRejected(
                    f"unsupported unary operator: {uop_type.__name__}"
                )
            return 1 + self._check_tree(node.operand, depth=depth + 1)
        raise CalculatorInputRejected(
            f"unsupported expression element: {type(node).__name__}"
        )

    def _check_source_nesting(self, expr: str) -> None:
        """Bracket nesting in the **source**, not just in the tree.

        ``ast`` erases redundant parentheses — ``(((1)))`` parses to a single
        ``Constant`` — so a tree-depth bound alone cannot see an expression
        whose nesting lives in the text. Walking the characters is linear and
        the source is already length-capped, so the lexical bound costs
        nothing and makes "deeply nested" mean what a caller expects.
        """
        depth = 0
        deepest = 0
        for char in expr:
            if char in "([{":
                depth += 1
                deepest = max(deepest, depth)
            elif char in ")]}":
                depth -= 1
        if deepest > self.MAX_AST_DEPTH:
            raise CalculatorInputRejected(
                f"expression nests {deepest} bracket level(s) deep; the limit is "
                f"{self.MAX_AST_DEPTH}"
            )

    def _preflight(self, expr: Any) -> ast.Expression:
        """Length, parse and shape — the cheapest possible refusal path."""
        if not isinstance(expr, str):
            raise CalculatorInputRejected("expression must be a string")
        if len(expr) > self.MAX_EXPRESSION_CHARS:
            raise CalculatorInputRejected(
                f"expression is {len(expr)} characters; the limit is "
                f"{self.MAX_EXPRESSION_CHARS}"
            )
        self._check_source_nesting(expr)
        try:
            tree = ast.parse(expr, mode="eval")
        except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
            raise CalculatorInputRejected(f"expression does not parse: {exc}") from exc
        if self._check_tree(tree, depth=0) > self.MAX_AST_NODES:
            raise CalculatorInputRejected(
                f"expression has more than {self.MAX_AST_NODES} nodes"
            )
        return tree

    # --- evaluation ---------------------------------------------------------

    def _eval_node(self, node: ast.AST, *, depth: int = 0) -> float | int:
        if isinstance(node, ast.Constant):
            return self._as_number(node.value)
        if isinstance(node, ast.BinOp):
            left = self._eval_node(node.left, depth=depth + 1)
            right = self._eval_node(node.right, depth=depth + 1)
            op_type = type(node.op)
            if op_type not in self._OPS:
                raise CalculatorInputRejected(
                    f"unsupported operator: {op_type.__name__}"
                )
            if op_type is ast.Pow:
                self._check_exponent(right)
                if isinstance(left, int):
                    # Deliberately conservative: bit_length(base) * exponent
                    # over-estimates the product's width, and over-estimating
                    # is the safe direction for a bound.
                    if (left.bit_length() or 1) * abs(right) > self.MAX_RESULT_BITS:
                        raise CalculatorInputRejected(
                            f"exponentiation would exceed {self.MAX_RESULT_BITS} bits"
                        )
            return self._guard_result(self._OPS[op_type](left, right))
        if isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand, depth=depth + 1)
            uop_type = type(node.op)
            if uop_type not in self._UNARY_OPS:
                raise CalculatorInputRejected(
                    f"unsupported unary operator: {uop_type.__name__}"
                )
            return self._guard_result(self._UNARY_OPS[uop_type](operand))
        raise CalculatorInputRejected(
            f"unsupported expression element: {type(node).__name__}"
        )

    async def execute(self, workspace_id: str, **kwargs: Any) -> Any:
        expr = kwargs.get("expression", "")
        if isinstance(expr, str) and not expr:
            return {"error": "Missing expression"}
        try:
            tree = self._preflight(expr)
            return {"result": self._eval_node(tree.body)}
        except CalculatorInputRejected as exc:
            return {
                "error": f"expression rejected: {exc.reason}",
                "error_code": exc.code,
            }
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
