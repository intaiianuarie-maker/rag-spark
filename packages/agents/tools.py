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
import json
import math
import operator
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .interfaces import AgentErrorCode

if TYPE_CHECKING:
    pass

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

#: T-V3-002 (V3 plan trace A1-1a): the search tool is a read-only retrieval
#: seam whose cost is also model-controlled. Every dimension is bounded
#: server-side before any retrieval happens:
#:
#: * :data:`MAX_QUERY_CHARS` bounds the query string itself — embedding is
#:   linear in tokens, lexical FTS is linear in query terms, and an
#:   unbounded query is unbounded work for three legs (dense/sparse/lexical).
#: * :data:`MAX_SEARCH_TOP_K` bounds the fan-out: each of dense/sparse/lexical
#:   asks a store for ``top_k`` rows, then fusion and an optional reranker
#:   touch every row again.
#: * :data:`MAX_SEARCH_OUTPUT_BYTES` and :data:`MAX_SNIPPET_CHARS` bound what
#:   the agent sees: without them a single large chunk (or many chunks) would
#:   exceed the run's token budget and the tool's output-bytes wall.
MAX_QUERY_CHARS = 500
MAX_SEARCH_TOP_K = 10
MAX_SEARCH_OUTPUT_BYTES = 16_384
MAX_SNIPPET_CHARS = 800


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
    cost_usd: float = 0.0

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
    cost_usd: float = 0.001

    def __init__(self, cost_usd: float | None = None) -> None:
        if cost_usd is not None:
            self.cost_usd = cost_usd
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
                raise CalculatorInputRejected(f"unsupported operator: {op_type.__name__}")
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
                raise CalculatorInputRejected(f"unsupported unary operator: {uop_type.__name__}")
            return 1 + self._check_tree(node.operand, depth=depth + 1)
        raise CalculatorInputRejected(f"unsupported expression element: {type(node).__name__}")

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
                f"expression is {len(expr)} characters; the limit is {self.MAX_EXPRESSION_CHARS}"
            )
        self._check_source_nesting(expr)
        try:
            tree = ast.parse(expr, mode="eval")
        except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
            raise CalculatorInputRejected(f"expression does not parse: {exc}") from exc
        if self._check_tree(tree, depth=0) > self.MAX_AST_NODES:
            raise CalculatorInputRejected(f"expression has more than {self.MAX_AST_NODES} nodes")
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
                raise CalculatorInputRejected(f"unsupported operator: {op_type.__name__}")
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
                raise CalculatorInputRejected(f"unsupported unary operator: {uop_type.__name__}")
            return self._guard_result(self._UNARY_OPS[uop_type](operand))
        raise CalculatorInputRejected(f"unsupported expression element: {type(node).__name__}")

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


class KnowledgeSearchTool(Tool):
    """ACL-scoped, read-only knowledge search (T-V3-002, trace A1-1a).

    The workspace is enforced **server-side**: the ``workspace_id`` comes from
    :class:`RunContext` (the first argument ``workspace_id`` to
    :meth:`execute`), never from ``kwargs``. A model-supplied
    ``workspace_id``/``workspace_ids``/``filter`` cannot steer the search out
    of the caller's workspace because:

    * the dispatch layer already rejects any ``workspace_id`` key in the tool
      payload as a reserved context field (``RESERVED_CONTEXT_FIELDS``), and
    * this tool **ignores** any such key even if it slipped through.

    The tool is **read-only** by construction: it only calls
    :meth:`RetrievalOrchestrator.retrieve` (no ``upsert``/``delete``/``index``
    surface exists on this path), and every store is reached through the
    ``retrieval.interfaces`` adapter boundary — no ``qdrant_client`` / vendor
    SDK is imported here.

    Output is **byte-bounded**: each snippet is truncated to
    :data:`MAX_SNIPPET_CHARS` and the final JSON is truncated until it fits in
    :data:`MAX_SEARCH_OUTPUT_BYTES`, so the runtime's ``max_output_bytes`` wall
    is never hit by a large corpus hit.
    """

    name: ClassVar[str] = "knowledge_search"
    description: ClassVar[str] = (
        "Search the knowledge base for evidence. Returns ACL-scoped snippets "
        "with provenance (document, chunk, score)."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_CHARS,
                "description": (
                    "Search query in natural language (1-500 characters). "
                    "The retrieval is hybrid (dense + lexical) and ACL-scoped."
                ),
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SEARCH_TOP_K,
                "description": (
                    f"Maximum number of evidence snippets to return "
                    f"(1-{MAX_SEARCH_TOP_K}, default 5)."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    MAX_QUERY_CHARS: ClassVar[int] = MAX_QUERY_CHARS
    MAX_SEARCH_TOP_K: ClassVar[int] = MAX_SEARCH_TOP_K
    MAX_SEARCH_OUTPUT_BYTES: ClassVar[int] = MAX_SEARCH_OUTPUT_BYTES
    MAX_SNIPPET_CHARS: ClassVar[int] = MAX_SNIPPET_CHARS
    cost_usd: float = 0.01

    def __init__(
        self, orchestrator: Any | None = None, cost_usd: float | None = None
    ) -> None:
        # ``Any`` to avoid importing the retrieval interface at runtime; the
        # only required surface is ``retrieve(query, acl_scope, context_top_k)``.
        self._orchestrator: Any | None = orchestrator
        if cost_usd is not None:
            self.cost_usd = cost_usd

    def _resolve_orchestrator(self) -> Any | None:
        if self._orchestrator is not None:
            return self._orchestrator
        # Lazy, best-effort construction for production use — never imports
        # qdrant_client here; the orchestrator factory owns the adapter wiring.
        try:
            from answer.factory import create_orchestrator

            return create_orchestrator()
        except Exception:
            return None

    @staticmethod
    def _snippet(text: str) -> str:
        if len(text) <= MAX_SNIPPET_CHARS:
            return text
        return text[: MAX_SNIPPET_CHARS - 3] + "..."

    @staticmethod
    def _fit_output(payload: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw.encode("utf-8")) <= MAX_SEARCH_OUTPUT_BYTES:
            return payload
        # Shrink by dropping the lowest-ranked hits until we fit.
        results = payload.get("results", [])
        while (
            results
            and len(json.dumps({**payload, "results": results}, ensure_ascii=False).encode("utf-8"))
            > MAX_SEARCH_OUTPUT_BYTES
        ):
            results = results[:-1]
        payload = {**payload, "results": results, "truncated": True}
        # If still too large (single huge hit), truncate its snippet.
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw.encode("utf-8")) > MAX_SEARCH_OUTPUT_BYTES and payload["results"]:
            first = dict(payload["results"][0])
            first["content"] = first["content"][:200] + "..."
            payload["results"][0] = first
        return payload

    async def execute(self, workspace_id: str, **kwargs: Any) -> Any:
        # Hard server-side workspace enforcement: ignore any model-supplied
        # workspace fields even if the dispatch guard were bypassed.
        kwargs.pop("workspace_id", None)
        kwargs.pop("workspace_ids", None)
        kwargs.pop("filter", None)
        kwargs.pop("acl_scope", None)

        query = kwargs.get("query", "")
        if not isinstance(query, str):
            return {
                "error": "query must be a string",
                "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
            }
        stripped = query.strip()
        if not stripped:
            return {
                "error": "query is required",
                "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
            }
        if len(query) > self.MAX_QUERY_CHARS:
            return {
                "error": f"query is {len(query)} characters; the limit is {self.MAX_QUERY_CHARS}",
                "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
            }
        top_k = kwargs.get("top_k", 5)
        if not isinstance(top_k, int) or isinstance(top_k, bool):
            return {
                "error": "top_k must be an integer",
                "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
            }
        if not 1 <= top_k <= self.MAX_SEARCH_TOP_K:
            return {
                "error": f"top_k must be between 1 and {self.MAX_SEARCH_TOP_K}",
                "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
            }

        orchestrator = self._resolve_orchestrator()
        if orchestrator is None:
            return {
                "error": "search not configured",
                "error_code": AgentErrorCode.TOOL_NOT_ALLOWED,
            }

        # ACL before ranking (R-004): scope is built server-side from the
        # RunContext workspace_id and never from model arguments.
        try:
            from core.auth import ACLScope

            scope = ACLScope(workspace_ids=[workspace_id])
        except Exception:
            # Fallback if core.auth is unavailable in a minimal test env.
            # Minimal ACL scope for test envs where core.auth is unavailable.
            scope = type(
                "ACLScope",
                (),
                {
                    "workspace_ids": [workspace_id],
                    "is_empty": False,
                    "include_public": False,
                },
            )()

        try:
            items = await orchestrator.retrieve(
                query=stripped,
                acl_scope=scope,
                context_top_k=top_k,
            )
        except Exception as exc:
            return {
                "error": f"search failed: {exc}",
                "error_code": AgentErrorCode.PROVIDER_ERROR,
            }

        # Defensive ACL re-filter: a store that ignored its filter still cannot leak.
        filtered: list[Any] = []
        for item in items:
            try:
                ws = item.workspace_id
            except AttributeError:
                ws = None
            if ws is not None and ws != workspace_id:
                continue
            filtered.append(item)
        # Respect top_k after re-filter.
        filtered = filtered[:top_k]

        results: list[dict[str, Any]] = []
        for item in filtered:
            try:
                content = item.content
            except AttributeError:
                content = ""
            try:
                score_val = item.score
            except AttributeError:
                score_val = 0.0
            try:
                ws_id = item.workspace_id
            except AttributeError:
                ws_id = workspace_id
            try:
                doc_id = item.document_id
            except AttributeError:
                doc_id = None
            try:
                chunk_id = item.chunk_id
            except AttributeError:
                chunk_id = None
            try:
                method_val = item.method
            except AttributeError:
                method_val = ""
            results.append(
                {
                    "content": self._snippet(str(content or "")),
                    "score": float(score_val or 0.0),
                    "workspace_id": ws_id,
                    "document_id": doc_id,
                    "chunk_id": chunk_id,
                    "method": str(method_val),
                }
            )

        payload: dict[str, Any] = {"results": results, "count": len(results)}
        return self._fit_output(payload)


class KnowledgeReadTool(Tool):
    """Scoped, read-only chunk reader (T-V3-003, trace A1-1b).

    Addresses one chunk by the exact triple ``(document_id, version_id, chunk_id)``
    with an ACL re-check **at read time**: the chunk quoted in one workspace
    must not leak into another via a direct read. The reader re-resolves the
    caller's scope against the chunk's owning workspace/version before returning
    bytes, and refuses revoked/deleted rows.

    The tool is **read-only** by construction: the only store operations on
    this path are ``SELECT``/``get`` via the persistence seam
    (``ingestion.persistence``) — no ``upsert``/``delete``/``index`` surface
    exists here, and no workspace-wide scan is performed (the lookup is by
    primary key, then the triple is verified).

    Output is **byte-bounded** with the same budget as search
    (:data:`MAX_SEARCH_OUTPUT_BYTES` / :data:`MAX_SNIPPET_CHARS`), so a large
    chunk cannot blow the run's token wall.
    """

    name: ClassVar[str] = "knowledge_read"
    description: ClassVar[str] = (
        "Read one chunk by its document/version/chunk triple. "
        "Returns the pinned version's text with provenance; ACL re-checked at read time."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "pattern": "^[0-9a-fA-F-]{36}$",
                "description": "Document UUID (exact).",
            },
            "document_version_id": {
                "type": "string",
                "pattern": "^[0-9a-fA-F-]{36}$",
                "description": "Document version UUID (exact, pinned).",
            },
            "chunk_id": {
                "type": "string",
                "pattern": "^[0-9a-fA-F-]{36}$",
                "description": "Chunk UUID (exact).",
            },
        },
        "required": ["document_id", "document_version_id", "chunk_id"],
        "additionalProperties": False,
    }

    MAX_OUTPUT_BYTES: ClassVar[int] = MAX_SEARCH_OUTPUT_BYTES
    MAX_SNIPPET_CHARS: ClassVar[int] = MAX_SNIPPET_CHARS
    cost_usd: float = 0.005

    def __init__(
        self, chunk_reader: Any | None = None, cost_usd: float | None = None
    ) -> None:
        # ``chunk_reader`` is an injectable async callable
        # ``(document_id, version_id, chunk_id) -> ChunkRow | None`` or a
        # minimal store with ``get_chunk``. When ``None`` the tool uses the
        # persistence seam directly.
        self._chunk_reader: Any | None = chunk_reader
        if cost_usd is not None:
            self.cost_usd = cost_usd

    @staticmethod
    def _is_uuid(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            import uuid as _uuid

            _uuid.UUID(value)
            return True
        except Exception:
            return False

    @staticmethod
    def _snippet(text: str) -> str:
        if len(text) <= MAX_SNIPPET_CHARS:
            return text
        return text[: MAX_SNIPPET_CHARS - 3] + "..."

    async def _fetch_via_reader(
        self, document_id: str, version_id: str, chunk_id: str
    ) -> Any | None:
        reader = self._chunk_reader
        if reader is None:
            return None
        # Support both callable and object with get_chunk
        try:
            if callable(reader):
                return await reader(document_id, version_id, chunk_id)
            if hasattr(reader, "get_chunk"):
                return await reader.get_chunk(document_id, version_id, chunk_id)
            if hasattr(reader, "get"):
                return await reader.get(document_id, version_id, chunk_id)
        except Exception:
            return None
        return None

    async def execute(self, workspace_id: str, **kwargs: Any) -> Any:
        # Hard server-side enforcement: pop any model-supplied scope fields.
        kwargs.pop("workspace_id", None)
        kwargs.pop("workspace_ids", None)
        kwargs.pop("filter", None)
        kwargs.pop("acl_scope", None)

        document_id = kwargs.get("document_id", "")
        version_id = kwargs.get("document_version_id", "")
        chunk_id = kwargs.get("chunk_id", "")

        for field_name, value in (
            ("document_id", document_id),
            ("document_version_id", version_id),
            ("chunk_id", chunk_id),
        ):
            if not isinstance(value, str) or not value.strip():
                return {
                    "error": f"{field_name} is required",
                    "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
                }
            if not self._is_uuid(value):
                return {
                    "error": f"{field_name} must be a UUID",
                    "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
                }

        # No workspace-wide scan: the only lookup is by the exact triple.
        # First try the injected reader (unit tests), then the persistence seam.
        chunk_row: Any | None = None
        # Injected path — used by unit tests, no DB required.
        if self._chunk_reader is not None:
            chunk_row = await self._fetch_via_reader(document_id, version_id, chunk_id)
            if chunk_row is None:
                return {
                    "error": "chunk not found for the given triple",
                    "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
                }
            # Re-check the triple exactly — no fuzzy fallback.
            try:
                row_doc = (
                    str(chunk_row.document_id)
                    if hasattr(chunk_row, "document_id")
                    else str(chunk_row.get("document_id", ""))
                )
            except Exception:
                row_doc = ""
            try:
                row_ver = (
                    str(chunk_row.document_version_id)
                    if hasattr(chunk_row, "document_version_id")
                    else str(chunk_row.get("document_version_id", ""))
                )
            except Exception:
                row_ver = ""
            try:
                row_cid = (
                    str(chunk_row.id) if hasattr(chunk_row, "id") else str(chunk_row.get("id", ""))
                )
            except Exception:
                row_cid = ""
            # Some fakes use chunk_id field instead of id
            if not row_cid or row_cid == "None":
                try:
                    row_cid = str(chunk_row.chunk_id)
                except Exception:
                    row_cid = ""
            if row_doc != document_id or row_ver != version_id or row_cid != chunk_id:
                return {
                    "error": "chunk triple mismatch — exact address required",
                    "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
                }
            # ACL re-check at read time
            try:
                row_ws = str(chunk_row.workspace_id)
            except Exception:
                try:
                    row_ws = str(chunk_row.get("workspace_id", ""))
                except Exception:
                    row_ws = ""
            if row_ws != workspace_id:
                return {
                    "error": "chunk not in caller's workspace",
                    "error_code": AgentErrorCode.WORKSPACE_MISMATCH,
                }
            # Revoked/deleted: check injected flags
            try:
                deleted = bool(chunk_row.deleted_at) if hasattr(chunk_row, "deleted_at") else False
            except Exception:
                deleted = False
            try:
                superseded = (
                    bool(chunk_row.superseded_at) if hasattr(chunk_row, "superseded_at") else False
                )
            except Exception:
                superseded = False
            # Also respect top-level flags injected by tests
            if chunk_row is not None and isinstance(chunk_row, dict):
                deleted = deleted or bool(chunk_row.get("deleted_at"))
                superseded = superseded or bool(chunk_row.get("superseded_at"))
                # Check version/document flags via extra dict keys
                if chunk_row.get("version_superseded_at") or chunk_row.get("document_deleted_at"):
                    superseded = True
                    deleted = True
            if deleted or superseded:
                return {
                    "error": "chunk version is revoked or deleted",
                    "error_code": AgentErrorCode.SOURCE_REVOKED,
                }
            # Build output from the fake row
            try:
                text_val = chunk_row.text
            except Exception:
                try:
                    text_val = chunk_row.get("text", "")
                except Exception:
                    text_val = ""
            snippet = self._snippet(str(text_val or ""))
            payload: dict[str, Any] = {
                "document_id": document_id,
                "document_version_id": version_id,
                "chunk_id": chunk_id,
                "workspace_id": workspace_id,
                "content": snippet,
                "truncated": len(str(text_val or "")) > self.MAX_SNIPPET_CHARS,
            }
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if len(raw) > self.MAX_OUTPUT_BYTES:
                payload["content"] = payload["content"][:200] + "..."
                payload["truncated"] = True
            return payload

        # Production path: persistence seam (read-only SELECTs).
        try:
            from ingestion.persistence import (
                ChunkRow,
                Document,
                DocumentVersion,
                get_session_factory,
            )

            factory = get_session_factory()
            async with factory() as session:
                # Exact triple: fetch chunk by primary key, then verify.
                chunk = await session.get(ChunkRow, uuid.UUID(chunk_id))
                if chunk is None:
                    return {
                        "error": "chunk not found",
                        "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
                    }
                if str(chunk.id) != chunk_id or str(chunk.document_version_id) != version_id:
                    return {
                        "error": "chunk triple mismatch — exact address required",
                        "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
                    }
                # ACL re-check
                if str(chunk.workspace_id) != workspace_id:
                    return {
                        "error": "chunk not in caller's workspace",
                        "error_code": AgentErrorCode.WORKSPACE_MISMATCH,
                    }
                version = await session.get(DocumentVersion, uuid.UUID(version_id))
                if version is None or str(version.document_id) != document_id:
                    return {
                        "error": "version not found or mismatched",
                        "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
                    }
                if version.superseded_at is not None:
                    return {
                        "error": "chunk version is revoked or deleted",
                        "error_code": AgentErrorCode.SOURCE_REVOKED,
                    }
                document = await session.get(Document, uuid.UUID(document_id))
                if document is None or str(document.workspace_id) != workspace_id:
                    return {
                        "error": "document not in caller's workspace",
                        "error_code": AgentErrorCode.WORKSPACE_MISMATCH,
                    }
                if document.deleted_at is not None:
                    return {
                        "error": "document is deleted",
                        "error_code": AgentErrorCode.SOURCE_REVOKED,
                    }
                if str(chunk.document_version_id) != str(version.id):
                    return {
                        "error": "chunk version mismatch",
                        "error_code": AgentErrorCode.TOOL_SCHEMA_INVALID,
                    }
                snippet = self._snippet(chunk.text or "")
                payload2: dict[str, Any] = {
                    "document_id": document_id,
                    "document_version_id": version_id,
                    "chunk_id": chunk_id,
                    "workspace_id": workspace_id,
                    "content": snippet,
                    "truncated": len(chunk.text or "") > self.MAX_SNIPPET_CHARS,
                }
                raw2 = json.dumps(payload2, ensure_ascii=False).encode("utf-8")
                if len(raw2) > self.MAX_OUTPUT_BYTES:
                    payload2["content"] = payload2["content"][:200] + "..."
                    payload2["truncated"] = True
                return payload2
        except Exception as exc:
            # Never leak raw DB errors; map to provider error with safe message.
            return {
                "error": f"read failed: {type(exc).__name__}",
                "error_code": AgentErrorCode.PROVIDER_ERROR,
            }


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
