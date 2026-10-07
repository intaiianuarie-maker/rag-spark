"""Membership-mutation lock contract (MICRO #7, test1's M1-001e suggestion).

Guards the M1-001d/M1-001e invariant: every code path that mutates a
``Membership`` row must hold the workspace row lock (``_lock_workspace``)
BEFORE any authorization/last-owner guard reads and before the mutation
itself, so a future fifth path can never reopen the ownerless race that the
demote/invite/accept/remove interleavings exposed.

Static AST scan over ``apps/api/app`` — deterministic, no races to lose:

* a function is *in scope* if it creates a membership (``Membership(...)``),
  writes an attribute of a membership-typed local (``membership.status = ...``),
  or passes one to ``session.add``/``session.delete``;
* membership-typed locals are bindings from a ``Membership(...)`` ctor or
  ``_get_membership``/``_require_membership`` calls (annotated ``Membership``
  bindings count too);
* in-scope functions must call ``_lock_workspace`` and the call must precede
  both the first mutation and the first guard read (``require``,
  ``ensure_owner_can_manage_owner``, ``ensure_last_owner_safe``,
  ``_count_owners``, ``_get_membership``, ``_require_membership``);
* ``_get_workspace`` is deliberately NOT a guard — the lock takes
  ``workspace.id``, so fetching the workspace row first is the required order.

Known gaps (documented, extend when the pattern appears): mutations hidden
behind an untyped helper call or an inner closure are not visible to this
scan; a differently-named lock helper would not be recognized.

Single allowlisted exception: the bootstrap owner insert in
``create_workspace`` — the workspace row is created in the same transaction,
so no concurrent mutator can see (or lock) that id yet.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "apps" / "api" / "app"

#: Calls that must happen under the lock (decision/row reads).
GUARD_CALLS = frozenset(
    {
        "require",
        "ensure_owner_can_manage_owner",
        "ensure_last_owner_safe",
        "_count_owners",
        "_get_membership",
        "_require_membership",
    }
)

#: Conventional parameter names treated as membership rows (unannotated style).
PARAM_NAME_HINTS = frozenset({"membership", "invitee"})
MEMBERSHIP_CTOR = "Membership"
TYPING_ACCESSORS = frozenset({"_get_membership", "_require_membership"})

#: (dotted module, function name) -> justification. Kept minimal on purpose.
ALLOWLIST_UNLOCKED = {
    (
        "app.auth.workspaces.service",
        "WorkspaceService.create_workspace",
    ): (
        "Bootstrap: the initial owner membership is inserted while the "
        "workspace row itself is being created in the same transaction — "
        "the id is not visible to any concurrent mutator, so there is "
        "nothing to serialize against yet."
    ),
}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _is_membership_ctor(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and _call_name(node) == MEMBERSHIP_CTOR
    )


def _module_dotted(path: Path) -> str:
    rel = path.relative_to(APP_ROOT.parent)  # apps/api/app/... -> app....
    return ".".join(rel.with_suffix("").parts)


def _membership_typed_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    typed: set[str] = set()
    # Parameters: annotated `Membership`, or conventionally named — a future
    # helper taking a membership row directly must not escape the contract.
    args = fn.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        params.append(args.vararg)
    if args.kwarg is not None:
        params.append(args.kwarg)
    for param in params:
        ann = param.annotation
        annotated = isinstance(ann, ast.Name) and ann.id == MEMBERSHIP_CTOR
        annotated = annotated or (
            isinstance(ann, ast.Attribute) and ann.attr == MEMBERSHIP_CTOR
        )
        if annotated or param.arg in PARAM_NAME_HINTS:
            typed.add(param.arg)
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None:
                continue
            if isinstance(value, ast.Await):
                value = value.value
            if not isinstance(value, ast.Call):
                continue
            name = _call_name(value)
            if name in TYPING_ACCESSORS or _is_membership_ctor(value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        typed.add(target.id)
    return typed


def _mutations(fn: ast.AST, typed: set[str]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in typed
                ):
                    found.append(
                        (node.lineno, f"attribute write `{target.value.id}.{target.attr}`")
                    )
        elif isinstance(node, ast.AugAssign):
            target = node.target
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in typed
            ):
                found.append((node.lineno, f"attribute write `{target.value.id}.{target.attr}`"))
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in {"add", "delete"}:
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in typed:
                        found.append((node.lineno, f"`{name}({arg.id})`"))
                    elif _is_membership_ctor(arg):
                        found.append((node.lineno, f"`{name}(Membership(...))`"))
            # Future-proof: a method invoked ON a membership row is treated as
            # a mutation unless someone adds a justified read-method exemption.
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in typed
            ):
                found.append(
                    (node.lineno, f"method call `{node.func.value.id}.{node.func.attr}()`")
                )
    return sorted(set(found))


def _calls_named(fn: ast.AST, names: frozenset[str] | set[str]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in names:
                out.append((node.lineno, name or "?"))
    return sorted(set(out))


def _display_path(path: Path) -> Path | str:
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:  # pragma: no cover - out-of-tree scan targets
        return path


def _qualname(fn: ast.FunctionDef | ast.AsyncFunctionDef, stack: list[str]) -> str:
    return ".".join([*stack, fn.name])


class _Collector(ast.NodeVisitor):
    """Collect membership mutators + their lock/guard/mutation line numbers."""

    def __init__(self, module: str, path: Path) -> None:
        self.module = module
        self.path = path
        self.stack: list[str] = []
        self.mutators: dict[str, list[tuple[int, str]]] = {}
        self.unlocked: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self.lock_lines: dict[str, int] = {}
        self.offenders: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._analyze(node)
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._analyze(node)
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _analyze(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        typed = _membership_typed_names(fn)
        muts = _mutations(fn, typed)
        if not muts:
            return
        qual = _qualname(fn, self.stack)
        key = f"{self.module}.{qual}"
        self.mutators[key] = muts

        locks = _calls_named(fn, frozenset({"_lock_workspace"}))
        if not locks:
            self.unlocked[(self.module, qual)] = muts
            return  # evaluated against the allowlist by the tests below
        lock_line = locks[0][0]
        self.lock_lines[key] = lock_line

        first_mut = muts[0][0]
        if lock_line > first_mut:
            self.offenders.append(
                f"{key} ({_display_path(self.path)}:{first_mut}): "
                f"_lock_workspace at line {lock_line} is AFTER the first mutation"
            )
        guards = [g for g in _calls_named(fn, GUARD_CALLS) if g[0] < lock_line]
        if guards:
            self.offenders.append(
                f"{key} ({_display_path(self.path)}): guard "
                f"`{guards[0][1]}` at line {guards[0][0]} runs BEFORE the lock "
                f"at {lock_line}"
            )


def _scan() -> _Collector:
    merged = _Collector(module="", path=APP_ROOT)
    for path in sorted(APP_ROOT.rglob("*.py")):
        module = _module_dotted(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _Collector(module=module, path=path)
        collector.visit(tree)
        merged.offenders.extend(collector.offenders)
        merged.mutators.update(collector.mutators)
        merged.unlocked.update(collector.unlocked)
        merged.lock_lines.update(collector.lock_lines)
    return merged


def test_membership_mutations_hold_the_workspace_lock_first() -> None:
    """The contract: locked-before-guards-and-mutation, zero unlocked paths."""
    scan = _scan()
    offenders = list(scan.offenders)
    for (module, qual), muts in scan.unlocked.items():
        if (module, qual) in ALLOWLIST_UNLOCKED:
            continue
        offenders.append(
            f"{module}.{qual}: membership mutation (line {muts[0][0]}) "
            f"without any _lock_workspace call — {muts[0][1]}"
        )
    assert not offenders, (
        "Membership mutation without the workspace row lock (ownerless-race risk):\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_detector_sees_the_four_canonical_mutators() -> None:
    """Anti-bit-rot: the scan must still recognize all four M1-001e paths.

    If a binding style changes (e.g. a new fetch helper) and the detector stops
    seeing a canonical mutator, THIS test fails — so the contract can never
    pass vacuously while a real path went dark.
    """
    scan = _scan()
    module = "app.auth.workspaces.service"
    for method in ("invite_member", "accept_invite", "change_role", "remove_member"):
        key = f"{module}.WorkspaceService.{method}"
        assert key in scan.mutators, f"detector missed canonical mutator {key}"
        assert key in scan.lock_lines, f"canonical mutator {key} not seen holding the lock"
    # and the bootstrap insert is detected as a mutator (it is one)…
    assert f"{module}.WorkspaceService.create_workspace" in scan.mutators
    # …but is the ONLY unlocked one
    assert set(scan.unlocked) == {(module, "WorkspaceService.create_workspace")}, (
        f"unexpected unlocked mutators: {sorted(scan.unlocked)}"
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Every allowlisted exception must still exist and still be unlocked."""
    scan = _scan()
    stale = set(ALLOWLIST_UNLOCKED) - set(scan.unlocked)
    assert not stale, (
        "Allowlist entries that no longer describe an unlocked mutator "
        f"(remove them): {sorted(stale)}"
    )
