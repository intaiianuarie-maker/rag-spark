"""Unit tests for T-V2-011: V2 turn-on procedure with preconditions & rollback."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts"))

from turn_on_v2_graph import (  # noqa: E402
    activate_v2_graph,
    check_v2_preconditions,
    rollback_v2_graph,
)

pytestmark = [pytest.mark.unit]


def test_v2_preconditions_pass_with_valid_configuration(tmp_path: Path) -> None:
    test_env = {
        "GRAPH_DB_PATH": str(tmp_path / "test_graph.db"),
        "GRAPH_MODE": "off",
    }
    result = check_v2_preconditions(test_env)
    assert result.passed is True
    assert result.error is None
    assert "db_path" in result.details
    assert result.details["model_free"] == "google/gemma-4-26b-a4b-it:free"


def test_v2_activation_flips_graph_mode(tmp_path: Path) -> None:
    test_env = {
        "GRAPH_DB_PATH": str(tmp_path / "test_graph.db"),
        "GRAPH_MODE": "off",
    }
    res = activate_v2_graph(target_mode="llm", env=test_env)
    assert res["status"] == "activated"
    assert res["previous_mode"] == "off"
    assert res["active_mode"] == "llm"
    assert test_env["GRAPH_MODE"] == "llm"


def test_v2_rollback_safely_reverts_to_off(tmp_path: Path) -> None:
    test_env = {
        "GRAPH_DB_PATH": str(tmp_path / "test_graph.db"),
        "GRAPH_MODE": "llm",
    }
    res = rollback_v2_graph(fallback_mode="off", env=test_env)
    assert res["status"] == "rolled_back"
    assert res["previous_mode"] == "llm"
    assert res["active_mode"] == "off"
    assert test_env["GRAPH_MODE"] == "off"
