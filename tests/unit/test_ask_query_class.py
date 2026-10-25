"""Unit: Ask query classification + honest router wiring (V1 Q9-prep).

Covers apps/api/app/ask/router.classify_query and its integration with
models.router_config.route: real query classes (never the "general"
placeholder), ANSWER_MODEL preferred honoring, and explicit override wins.
No services required.

T-V1-038 call-site pins (handler-level, fake engine, no services): every
request resolves its model through the policy router — configured operator
default vs permitted override vs unconfigured (3/3), the workspace-rules seam,
and the recorded selection reason (identifiers + tier only, never prompt
content). Source pins assert the handler path carries no model literal.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
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

from answer.models import Answer  # noqa: E402
from app.ask.router import (  # noqa: E402
    AskRequest,
    ask,
    classify_query,
    load_model_config,
    resolve_model,
    selection_reason,
)
from core.auth import Principal, Role  # noqa: E402
from models.router_config import (  # noqa: E402
    FALLBACK_MODEL,
    LOCAL_ONLY_MODEL_ENV,
    REQUIRED_CAPABILITY,
    RoutingDimension,
    RoutingRule,
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


# ---------------------------------------------------------------------------
# T-V1-038 — the policy router is the only model source on the Ask path
# ---------------------------------------------------------------------------

#: What `build_answer_engine()` leaves on the engine if the router never runs.
FACTORY_SENTINEL = "factory-model-never-routed"

ASK_LOGGER = "knowledge_os.api.ask"


class _FakeEngine:
    """AnswerEngine stand-in: no services, no network, model starts unrouted."""

    def __init__(self) -> None:
        self.model = FACTORY_SENTINEL
        self.queries: list[str] = []

    async def answer(self, *, query, acl_scope, mode=None, **kwargs) -> Answer:
        self.queries.append(query)
        return Answer(answer="ok")


async def _ask(monkeypatch, *, override_model: str | None = None, query: str | None = None):
    """Call the real handler with a fake engine (no DB, no gateway)."""
    import app.ask.router as router_mod

    engine = _FakeEngine()

    def _fake_factory(*, model: str, sensitive: bool = False):
        # T-V1-028: the handler must thread the router's pick into the factory
        # as its REQUIRED model — capture it so tests can assert the exact
        # identity that would reach the provider.
        engine.model = model
        return engine

    monkeypatch.setattr(router_mod, "build_answer_engine", _fake_factory)
    workspace_id = uuid.uuid4()
    principal = Principal(
        user_id="u-tv1-038",
        is_authenticated=True,
        workspace_roles={str(workspace_id): Role.OWNER},
    )
    answer = await ask(
        workspace_id,
        AskRequest(query=query or "What is the budget?", model=override_model),
        principal,
    )
    return engine, answer


def _events(caplog) -> list:
    return [r for r in caplog.records if getattr(r, "event", None) == "ask_model_selected"]


async def test_ask_resolves_configured_operator_default(monkeypatch, caplog):
    """Case 1/3: operator default (ANSWER_MODEL) — configured, no override."""
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)

    engine, answer = await _ask(monkeypatch)

    assert engine.model == "operator-model-a"
    assert engine.model != FACTORY_SENTINEL  # router ran: 0 bypasses
    assert answer.answer == "ok"
    assert len(engine.queries) == 1
    events = _events(caplog)
    assert len(events) == 1
    assert events[0].reason == "operator_default"
    assert events[0].model == "operator-model-a"
    assert events[0].override_supplied is False


async def test_ask_permitted_override_beats_operator_default(monkeypatch, caplog):
    """Case 2/3: caller override is honored and labeled as such."""
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)

    engine, _ = await _ask(monkeypatch, override_model="request-override-b")

    assert engine.model == "request-override-b"
    assert engine.model != FACTORY_SENTINEL
    events = _events(caplog)
    assert len(events) == 1
    assert events[0].reason == "permitted_override"
    assert events[0].model == "request-override-b"
    assert events[0].override_supplied is True


async def test_ask_unconfigured_falls_back_to_policy_default(monkeypatch, caplog):
    """Case 3/3: no ANSWER_MODEL — the default comes from the policy module."""
    monkeypatch.delenv("ANSWER_MODEL", raising=False)
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)

    engine, _ = await _ask(monkeypatch)

    assert engine.model == FALLBACK_MODEL  # policy-owned fallback, not a handler literal
    assert engine.model != FACTORY_SENTINEL
    events = _events(caplog)
    assert len(events) == 1
    assert events[0].reason == "operator_default_unconfigured"
    assert events[0].model == FALLBACK_MODEL


def test_ask_load_model_config_reads_the_operator_default_tier(monkeypatch):
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    configured = load_model_config("ws-1", override_model=None)
    assert configured.preferred_model == "operator-model-a"
    assert configured.routing_rules == ()  # workspace seam: empty, not fabricated

    monkeypatch.delenv("ANSWER_MODEL", raising=False)
    assert load_model_config("ws-1", override_model=None).preferred_model is None


def test_ask_workspace_rules_seam_selects_and_is_labeled(monkeypatch):
    """The workspace tier, once loaded, flows through `route()` untouched."""
    rules = (
        RoutingRule(RoutingDimension.CAPABILITY, "chat", priority=1),
        RoutingRule(RoutingDimension.COST, "medium", priority=2),
    )

    def _with_rules(workspace_id: str, *, override_model: str | None) -> WorkspaceModelConfig:
        return WorkspaceModelConfig(
            workspace_id=workspace_id,
            preferred_model="preferred-model",
            override_model=override_model,
            routing_rules=rules,
        )

    monkeypatch.setattr("app.ask.router.load_model_config", _with_rules)
    model, reason = resolve_model("ws-1", query_class="simple", override_model=None)
    assert model == "o3-mini"  # cost=medium among chat-capable models
    assert model != "preferred-model"
    assert reason == "workspace_rules"


def test_ask_policy_precedence_keeps_override_above_rules(monkeypatch):
    """Disclosed discrepancy pin (see handoff): MICRO-TICKETS-V1 T-V1-038 words
    the precedence as workspace > override > default, while the landed policy
    (`models.router_config.route`, TASK-M7-001a / T-V1-028) orders
    veto > override > workspace rules > default. The call site must not
    re-implement or re-order precedence (that would be a second, diverging
    policy), so it defers to `route()` and this pins the policy's real order
    for the coordinator to rule on.
    """
    rules = (RoutingRule(RoutingDimension.CAPABILITY, "chat", priority=1),)

    def _with_rules(workspace_id: str, *, override_model: str | None) -> WorkspaceModelConfig:
        return WorkspaceModelConfig(
            workspace_id=workspace_id,
            preferred_model="preferred-model",
            override_model=override_model,
            routing_rules=rules,
        )

    monkeypatch.setattr("app.ask.router.load_model_config", _with_rules)
    model, reason = resolve_model("ws-1", query_class="simple", override_model="request-override-b")
    assert model == "request-override-b"
    assert reason == "permitted_override"


def test_ask_selection_reason_labels_the_veto_tier(monkeypatch):
    # T-V1-029 re-pin: the veto tier resolves to the explicitly allowed local
    # model (a sensitive workspace with no local model configured now raises
    # LocalOnlyUnavailable before any reason can be recorded).
    monkeypatch.setenv(LOCAL_ONLY_MODEL_ENV, "llama3.1:8b")
    config = WorkspaceModelConfig(workspace_id="ws-1", sensitive=True, override_model="x")
    assert selection_reason(config, "simple", "local-only") == "policy_veto_local_only"


async def test_ask_reason_log_has_no_prompt_content(monkeypatch, caplog):
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)
    query = "What is the secret budget figure?"

    await _ask(monkeypatch, query=query)

    events = _events(caplog)
    assert len(events) == 1
    blob = " ".join(
        str(value)
        for record in caplog.records
        for value in [record.getMessage(), *record.__dict__.values()]
    )
    for fragment in (query, "secret budget", "What is the"):
        assert fragment not in blob
    # What IS logged: identifiers + tier, nothing else.
    assert events[0].query_class == "simple"
    assert isinstance(events[0].workspace_id, str)
    assert events[0].reason in {
        "policy_veto_local_only",
        "permitted_override",
        "workspace_rules",
        "operator_default",
        "operator_default_unconfigured",
    }


def test_ask_handler_path_has_no_hardcoded_model():
    import app.ask.router as router_mod

    source = Path(router_mod.__file__).read_text(encoding="utf-8")
    assert "gpt-4o-mini" not in source  # no model literal anywhere in the call site
    assert "DEFAULT_ANSWER_MODEL" not in source  # handler does not import a default
    # Every model selection on the handler path goes through the ONE policy
    # resolver. TASK-M4-001b added the streaming endpoint, so the call-site
    # count is now exactly 2 (POST /ask + POST /stream) — a THIRD site would
    # be a second, potentially diverging policy and must fail this pin.
    assert source.count("resolved_model, reason = resolve_model(") == 2
    # The router's pick is threaded into the factory as its REQUIRED model, and
    # there is no second assignment that could overwrite it (T-V1-028 single
    # source) — at BOTH call sites.
    assert (
        source.count("build_answer_engine(model=resolved_model, sensitive=_egress_sensitive)") == 2
    )
    assert "engine.model =" not in source
