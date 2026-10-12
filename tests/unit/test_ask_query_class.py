"""Unit: Ask query classification + honest router wiring (V1 Q9-prep).

Covers apps/api/app/ask/router.classify_query and its integration with
models.router_config.route: real query classes (never the "general"
placeholder), ANSWER_MODEL preferred honoring, and explicit override wins.
No services required.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
for _p in (REPO / "packages", REPO / "apps" / "api"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.unit

from app.ask.router import classify_query  # noqa: E402
from models.router_config import (  # noqa: E402
    REQUIRED_CAPABILITY,
    WorkspaceModelConfig,
    route,
)


def test_classify_returns_only_real_router_classes():
    queries = [
        "what governs access?",
        "```python\nprint('hi')\n```",
        "def foo(): pass",
        "Capitala Franței este Paris?",
        "Explică de ce lansarea a fost aprobată și compară bugetele?",
        "Why did the launch fail and how should we compare the two budgets in detail today?",
        "one two three four five six seven eight nine ten eleven twelve thirteen?",
    ]
    for q in queries:
        assert classify_query(q) in REQUIRED_CAPABILITY, q


def test_classify_code_wins():
    assert classify_query("```python\nprint(1)\n```") == "code"
    assert classify_query("def deploy():\n    pass") == "code"


def test_classify_multilingual_diacritics():
    assert classify_query("Capitala Franței este Paris?") == "multilingual"
    assert classify_query("Secção de Lisboa?") == "multilingual"


def test_classify_complex_and_simple():
    assert classify_query("Why did the launch fail and explain the causes?") == "complex"
    assert classify_query("What is the budget?") == "simple"


def test_route_honors_answer_model_preferred(monkeypatch):
    monkeypatch.setenv("ANSWER_MODEL", "my-custom-model")
    import app.ask.router as router_mod

    # The route wiring must read ANSWER_MODEL as preferred; replicate the
    # construction the endpoint performs.
    preferred = os.environ.get("ANSWER_MODEL", "gpt-4o-mini")
    config = WorkspaceModelConfig(workspace_id="ws-1", preferred_model=preferred)
    assert route(classify_query("What is the budget?"), config) == "my-custom-model"
    assert router_mod.classify_query("What is the budget?") == "simple"


def test_route_override_wins_over_preferred():
    config = WorkspaceModelConfig(
        workspace_id="ws-1",
        override_model="override-model",
        preferred_model="preferred-model",
    )
    assert route("simple", config) == "override-model"
    assert route("code", config) == "override-model"


def test_unknown_placeholder_still_falls_through():
    # Regression guard: the old "general" placeholder must never be produced
    # by classify_query (it disables rule selection by design).
    assert "general" not in REQUIRED_CAPABILITY
    for q in ("hello", "```x```", "Bună ziua", "why and how in detail?"):
        assert classify_query(q) != "general"
