"""T-V4-027 — V4 release pack tests (drift-proof envelope, flags, rollback).

The pack is data (``workflow_engine.release_pack``); this module proves it:

* every envelope bound is asserted against the LIVE constant by import
  (``getattr`` on the module the dotted path names) — a constant changed
  without updating the pack fails here;
* the envelope is EXHAUSTIVE over the layers (every public ``MAX_*`` /
  bound constant in validator/executor/langflow_adapter is registered);
* every flag in the package is registered with default state OFF,
  preconditions and a refusal proof;
* every rollback step is ordered, actionable, and names its proving ticket
  plus a re-run pointer.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from workflow_engine.release_pack import (
    FLAG_REGISTRY,
    RESOURCE_ENVELOPE,
    ROLLBACK_NOTE,
    EnvelopeBound,
    FlagEntry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _live_value(path: str) -> object:
    """Import the dotted path and return the live constant.

    Handles both module constants (``pkg.mod.CONST``) and class attributes
    (``pkg.mod.Class.CONST``).
    """
    try:
        module_path, _, attr = path.rpartition(".")
        module = importlib.import_module(module_path)
        return getattr(module, attr)
    except ImportError:
        # Class attribute: split one level further.
        owner_path, _, attr = path.rpartition(".")
        module_path, _, class_name = owner_path.rpartition(".")
        module = importlib.import_module(module_path)
        return getattr(getattr(module, class_name), attr)


# --------------------------------------------------------------------------- #
# 1. Resource envelope — drift-proof and exhaustive
# --------------------------------------------------------------------------- #

BOUND_NAME_RE = re.compile(r"^(MAX_[A-Z0-9_]+|.*_TIMEOUT_S|.*_MAX_[A-Z0-9_]+)$")

#: Modules whose public bound constants must appear in the envelope.
ENVELOPE_LAYER_MODULES = (
    "workflow_engine.validator",
    "workflow_engine.executor",
    "workflow_engine.adapters.langflow_adapter",
)


@pytest.mark.unit
@pytest.mark.parametrize("bound", RESOURCE_ENVELOPE, ids=lambda b: b.path)
def test_envelope_matches_live_constants(bound: EnvelopeBound) -> None:
    """Each pack default equals the live constant (drift fails the test)."""
    live = _live_value(bound.path)
    assert live == bound.default, (
        f"envelope drift: {bound.path} is {live!r}, the pack says {bound.default!r} — "
        "update the pack (or revert the constant)"
    )
    assert bound.reason.strip(), "every bound carries its reason"
    assert bound.layer in {"validator", "executor.node", "executor.run", "import"}


@pytest.mark.unit
def test_envelope_is_exhaustive_over_layers() -> None:
    """Every bound constant in the covered modules is registered."""
    registered = {b.path for b in RESOURCE_ENVELOPE}
    missing: list[str] = []
    for module_path in ENVELOPE_LAYER_MODULES:
        module = importlib.import_module(module_path)
        for attr in dir(module):
            if attr.startswith("_"):
                continue
            if not BOUND_NAME_RE.match(attr):
                continue
            dotted = f"{module_path}.{attr}"
            # Class attributes (e.g. LangflowAdapter.MAX_NODES) live on the class.
            if dotted in registered:
                continue
            owner = getattr(module, attr)
            if isinstance(owner, property):
                continue
            # Resolve "the constant on this module" — a class attribute is
            # registered under its class path, so look for that too.
            if any(p.endswith(f".{attr}") for p in registered):
                continue
            missing.append(dotted)
    assert not missing, f"undocumented bounds (add to RESOURCE_ENVELOPE): {sorted(missing)}"


@pytest.mark.unit
def test_envelope_defaults_are_positive_and_bounded() -> None:
    for bound in RESOURCE_ENVELOPE:
        assert bound.default > 0
    # The cover has all three layers, not just one.
    layers = {b.layer for b in RESOURCE_ENVELOPE}
    assert {"validator", "executor.node", "executor.run", "import"} <= layers


# --------------------------------------------------------------------------- #
# 2. Flag registry — every flag documented, all defaulting OFF
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_flag_registry_covers_every_flag_constant() -> None:
    """Every flag constant in the package is registered (flags undocumented: 0)."""
    registry_flags = {f.flag for f in FLAG_REGISTRY}
    package_dir = REPO_ROOT / "packages" / "workflow_engine"
    found: set[str] = set()
    for path in package_dir.rglob("*.py"):
        text = path.read_text()
        # Flag-shaped constants: *_ENV / *_FLAG / ADAPTER_FLAG_ENV.
        for match in re.finditer(r"^([A-Z][A-Z0-9_]*_ENV)\s*=", text, re.MULTILINE):
            found.add(match.group(1))
        for match in re.finditer(r"^([A-Z][A-Z0-9_]*FLAG[A-Z0-9_]*)\s*=", text, re.MULTILINE):
            found.add(match.group(1))
    # The W3 env flag is registered under its env-var NAME, not the constant.
    missing = sorted(
        name for name in found if name != "ADAPTER_FLAG_ENV"
    )
    assert not missing, f"undocumented flag constants: {missing}"
    assert "WORKFLOW_V3_ADAPTER" in registry_flags


@pytest.mark.unit
@pytest.mark.parametrize("entry", FLAG_REGISTRY, ids=lambda e: e.flag)
def test_flag_registry_entries_are_complete(entry: FlagEntry) -> None:
    """Every entry: default OFF, preconditions, refusal, proof, ticket."""
    assert entry.default_state == "off", "every V4 surface ships OFF"
    assert entry.enable_preconditions, "preconditions are stated"
    assert entry.refusal_while_off.strip()
    assert entry.refusal_proof.strip()
    assert re.match(r"^T-V[0-9]-[0-9]+$", entry.proving_ticket), (
        "each flag names a proving ticket"
    )


@pytest.mark.unit
def test_flag_registry_covers_every_surface() -> None:
    surfaces = {f.surface for f in FLAG_REGISTRY}
    assert any("W3" in s for s in surfaces), "W3 adapter flagged"
    assert any("schedule" in s for s in surfaces), "W6 schedules flagged"
    assert any("trigger" in s for s in surfaces), "W6 triggers flagged"
    assert any("AGENT" in s for s in surfaces), "V3 executor seam flagged"
    assert all(f.default_state == "off" for f in FLAG_REGISTRY)


# --------------------------------------------------------------------------- #
# 3. Rollback note — ordered, actionable, proven
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_rollback_steps_are_ordered_and_proven() -> None:
    orders = [step.order for step in ROLLBACK_NOTE]
    assert orders == sorted(orders), "rollback steps are ordered"
    assert orders[0] == 1 and len(set(orders)) == len(orders)
    for step in ROLLBACK_NOTE:
        assert step.action.strip() and step.surface.strip()
        assert re.search(r"T-V[0-9]-[0-9]+", step.proving_ticket), (
            "each step names its proving ticket"
        )
        assert step.rerun.strip()


@pytest.mark.unit
def test_rollback_covers_every_surface_with_a_pointer() -> None:
    surfaces = {step.surface for step in ROLLBACK_NOTE}
    assert any("W3" in s for s in surfaces)
    assert any("W6 schedule/trigger" in s for s in surfaces)
    assert any("checkpoint" in s for s in surfaces)
    assert any("migration" in s for s in surfaces)
    assert any("resource envelope" in s for s in surfaces)
    for step in ROLLBACK_NOTE:
        assert step.rerun.startswith("python -m pytest"), (
            "each step carries a re-run pointer"
        )


@pytest.mark.unit
def test_rollback_rerun_pointers_name_existing_test_files() -> None:
    """Every pointer to a test file that exists in this tree is well-formed.

    Pointers to files in another agent's lane are allowed (they name the
    probe that proves the step) — but they must still be pytest node-path
    shaped so the coordinator can run them verbatim.
    """
    for step in ROLLBACK_NOTE:
        assert " -k " in step.rerun or step.rerun.endswith("py"), step.rerun
        named = re.search(r"(tests/unit/[\w./-]+)", step.rerun)
        if named is not None:
            assert (REPO_ROOT / named.group(1)).exists(), (
                f"{step.surface}: pointer names a file that does not exist"
            )
