"""T-V1-028 — workspace model policy actually loads; precedence LOCKED.

Pins the three precedence levels end-to-end (workspace > permitted override >
operator default), the authoritative ``WORKSPACE_MODEL_CONFIG`` loader, and the
recorded selection reason + provider identity — with no secrets in the log.

Reconciles the T-V1-038 wording discrepancy: MICRO-TICKETS-V1 words the tiers
*workspace > override > default*, but the landed policy orders the caller's
permitted override ABOVE the workspace rules. The coordinator ruling
("override-above-rules stays") is LOCKED here:
    veto > permitted override > workspace rules > operator default.
No services required — handler-level with a fake engine, policy-level direct.
"""

from __future__ import annotations

import json
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
    load_model_config,
    resolve_model,
)
from core.auth import Principal, Role  # noqa: E402
from models.router_config import (  # noqa: E402
    FALLBACK_MODEL,
    LOCAL_ONLY_MODEL_ENV,
    PRECEDENCE_ORDER,
    WORKSPACE_MODEL_CONFIG_ENV,
    LocalOnlyUnavailable,
    RoutingDimension,
    RoutingRule,
    load_workspace_config,
    provider_identity,
    route,
    route_decision,
)

ASK_LOGGER = "knowledge_os.api.ask"
_WS = "ws-tv1-028"
#: Deterministic workspace id for handler-level tests, so a WORKSPACE_MODEL_CONFIG
#: store keyed by ``str(ASK_WS_ID)`` matches what the real handler threads through.
ASK_WS_ID = uuid.uuid4()


def _rules(*pairs: tuple[str, str]) -> tuple[RoutingRule, ...]:
    return tuple(
        RoutingRule(RoutingDimension(dim), val, priority=i + 1)
        for i, (dim, val) in enumerate(pairs)
    )


# ---------------------------------------------------------------------------
# 1. The LOCKED precedence order (override-above-rules stays)
# ---------------------------------------------------------------------------


def test_precedence_order_is_locked_override_above_rules():
    """The single authoritative statement of precedence. Highest tier first.

    This tuple is the contract; changing it is a policy decision, not a refactor.
    """
    assert PRECEDENCE_ORDER == (
        "policy_veto_local_only",
        "permitted_override",
        "workspace_rules",
        "operator_default",
    )


def test_permitted_override_beats_workspace_rules_in_policy():
    """Coordinator ruling: the caller's permitted override sits ABOVE workspace rules."""
    config_ws = type(load_model_config(_WS, override_model=None))(
        workspace_id=_WS,
        override_model="override-x",
        preferred_model="operator-default",
        routing_rules=_rules(("capability", "chat"), ("cost", "high")),
    )
    model, reason = route_decision("simple", config_ws)
    assert model == "override-x"
    assert reason == "permitted_override"
    # The workspace rules alone WOULD have picked something else (proves override won).
    rules_only = type(config_ws)(
        workspace_id=_WS,
        override_model=None,
        preferred_model="operator-default",
        routing_rules=config_ws.routing_rules,
    )
    assert route("simple", rules_only) != "override-x"


def test_veto_beats_everything_and_is_never_overridden(monkeypatch):
    """T-V1-029 re-pin: the veto resolves to the explicitly allowed local
    model (never the ``local-only`` sentinel as a model name, never the
    override/rules/preferred tiers)."""
    monkeypatch.setenv(LOCAL_ONLY_MODEL_ENV, "llama3.1:8b")
    config = type(load_model_config(_WS, override_model=None))(
        workspace_id=_WS,
        sensitive=True,
        override_model="override-x",
        routing_rules=_rules(("capability", "chat")),
    )
    assert route_decision("simple", config) == ("llama3.1:8b", "policy_veto_local_only")


def test_veto_fails_closed_without_a_configured_local_model():
    """T-V1-029: no explicitly allowed local model ⇒ documented unavailable,
    not a routable sentinel/cloud model."""
    config = type(load_model_config(_WS, override_model=None))(
        workspace_id=_WS,
        sensitive=True,
        override_model="override-x",
        routing_rules=_rules(("capability", "chat")),
    )
    with pytest.raises(LocalOnlyUnavailable):
        route_decision("simple", config)


def test_route_decision_reason_matches_route_selection():
    """route() and route_decision()[0] can never disagree (same policy pass)."""
    for override in (None, "override-x"):
        config = type(load_model_config(_WS, override_model=override))(
            workspace_id=_WS,
            override_model=override,
            preferred_model="operator-default",
            routing_rules=_rules(("capability", "chat")),
        )
        assert route("simple", config) == route_decision("simple", config)[0]


# ---------------------------------------------------------------------------
# 2. Authoritative workspace config actually loads
# ---------------------------------------------------------------------------


def test_workspace_config_loads_from_env_store(monkeypatch):
    store = {_WS: {"routing_rules": [{"dimension": "capability", "value": "chat", "priority": 1}]}}
    monkeypatch.setenv(WORKSPACE_MODEL_CONFIG_ENV, json.dumps(store))
    loaded = load_workspace_config(_WS)
    assert loaded is not None
    assert loaded.routing_rules == _rules(("capability", "chat"))


def test_workspace_config_flows_into_handler_tier(monkeypatch):
    """The loaded workspace rules reach `route()` via load_model_config (not stubbed)."""
    store = {_WS: {"routing_rules": [{"dimension": "cost", "value": "medium", "priority": 1}]}}
    monkeypatch.setenv(WORKSPACE_MODEL_CONFIG_ENV, json.dumps(store))
    monkeypatch.delenv("ANSWER_MODEL", raising=False)
    config = load_model_config(_WS, override_model=None)
    assert config.routing_rules == _rules(("cost", "medium"))
    model, reason = route_decision("simple", config)
    assert reason == "workspace_rules"
    assert model == "o3-mini"  # medium-cost chat model among registry


def test_workspace_config_absent_degrades_to_no_rules(monkeypatch):
    monkeypatch.delenv(WORKSPACE_MODEL_CONFIG_ENV, raising=False)
    assert load_workspace_config(_WS) is None
    assert load_model_config(_WS, override_model=None).routing_rules == ()


def test_workspace_config_unparseable_degrades_safely(monkeypatch):
    monkeypatch.setenv(WORKSPACE_MODEL_CONFIG_ENV, "{not json")
    assert load_workspace_config(_WS) is None  # no crash, no fabricated rules


def test_workspace_config_ignores_other_workspaces(monkeypatch):
    store = {"other-ws": {"routing_rules": [{"dimension": "cost", "value": "high", "priority": 1}]}}
    monkeypatch.setenv(WORKSPACE_MODEL_CONFIG_ENV, json.dumps(store))
    assert load_workspace_config(_WS) is None


# ---------------------------------------------------------------------------
# 3. Handler pins all three precedence levels (via the real handler)
# ---------------------------------------------------------------------------


class _FakeEngine:
    def __init__(self) -> None:
        self.model: str | None = None
        self.queries: list[str] = []

    async def answer(self, *, query, acl_scope, mode=None, **kwargs) -> Answer:
        self.queries.append(query)
        return Answer(answer="ok")


async def _ask(monkeypatch, *, override_model: str | None = None):
    import app.ask.router as router_mod

    engine = _FakeEngine()

    def _factory(*, model: str):
        engine.model = model
        return engine

    monkeypatch.setattr(router_mod, "build_answer_engine", _factory)
    workspace_id = ASK_WS_ID
    principal = Principal(
        user_id="u-tv1-028",
        is_authenticated=True,
        workspace_roles={str(workspace_id): Role.OWNER},
    )
    await ask(
        workspace_id, AskRequest(query="What is the budget?", model=override_model), principal
    )
    return engine


def _events(caplog) -> list:
    return [r for r in caplog.records if getattr(r, "event", None) == "ask_model_selected"]


async def test_level_operator_default(monkeypatch, caplog):
    monkeypatch.delenv(WORKSPACE_MODEL_CONFIG_ENV, raising=False)
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)
    engine = await _ask(monkeypatch)
    assert engine.model == "operator-model-a"
    assert _events(caplog)[0].reason == "operator_default"


async def test_level_permitted_override(monkeypatch, caplog):
    monkeypatch.delenv(WORKSPACE_MODEL_CONFIG_ENV, raising=False)
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)
    engine = await _ask(monkeypatch, override_model="request-override-b")
    assert engine.model == "request-override-b"
    assert _events(caplog)[0].reason == "permitted_override"


async def test_level_workspace_rules_load_and_win_over_default(monkeypatch, caplog):
    store = {
        str(ASK_WS_ID): {
            "routing_rules": [{"dimension": "cost", "value": "medium", "priority": 1}]
        }
    }
    monkeypatch.setenv(WORKSPACE_MODEL_CONFIG_ENV, json.dumps(store))
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)
    engine = await _ask(monkeypatch)
    assert engine.model == "o3-mini"  # workspace rules beat the operator default
    assert engine.model != "operator-model-a"
    assert _events(caplog)[0].reason == "workspace_rules"


async def test_unconfigured_falls_back_to_policy_fallback(monkeypatch, caplog):
    monkeypatch.delenv(WORKSPACE_MODEL_CONFIG_ENV, raising=False)
    monkeypatch.delenv("ANSWER_MODEL", raising=False)
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)
    engine = await _ask(monkeypatch)
    assert engine.model == FALLBACK_MODEL
    assert _events(caplog)[0].reason == "operator_default_unconfigured"


# ---------------------------------------------------------------------------
# 4. Provider identity recorded; no secrets in reason logs
# ---------------------------------------------------------------------------


def test_provider_identity_is_non_secret():
    assert provider_identity(FALLBACK_MODEL) == "litellm"
    assert provider_identity("nvidia/nemotron-3.5-lightning:free") == "openrouter"
    # Never leaks host/url/credential material.
    for model in (FALLBACK_MODEL, "openai/gpt-4o-mini", "unknown-model"):
        ident = provider_identity(model)
        assert ident in {"litellm", "openrouter"}
        assert "http" not in ident and "key" not in ident.lower()


async def test_log_records_provider_identity_and_no_secrets(monkeypatch, caplog):
    monkeypatch.delenv(WORKSPACE_MODEL_CONFIG_ENV, raising=False)
    monkeypatch.setenv("ANSWER_MODEL", "operator-model-a")
    # Plant a credential that must never appear in any log record.
    monkeypatch.setenv("LITELLM_API_KEY", "sk-super-secret-token-XYZ")
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)
    query = "What is the confidential budget figure?"

    await _ask(monkeypatch)

    events = _events(caplog)
    assert len(events) == 1
    assert events[0].model == "operator-model-a"
    assert events[0].provider == provider_identity("operator-model-a")
    assert events[0].reason == "operator_default"

    blob = " ".join(
        str(v)
        for rec in caplog.records
        for v in [rec.getMessage(), *rec.__dict__.values()]
    )
    # No prompt content, no credential.
    for forbidden in (query, "confidential budget", "What is the", "sk-super-secret", "XYZ"):
        assert forbidden not in blob


def test_handler_path_has_no_model_literal_or_default(monkeypatch):
    import app.ask.router as router_mod

    source = Path(router_mod.__file__).read_text(encoding="utf-8")
    assert "gpt-4o-mini" not in source
    assert "DEFAULT_ANSWER_MODEL" not in source
    assert "engine.model =" not in source


def test_factory_has_no_silent_default_model():
    source = (REPO / "packages" / "answer" / "factory.py").read_text(encoding="utf-8")
    # Strip docstrings/comments so we assert on CODE, not prose explaining the removal.
    import ast

    tree = ast.parse(source)
    code_segments = [
        ast.get_source_segment(source, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.FunctionDef | ast.Call)
    ]
    code = "\n".join(s for s in code_segments if s)
    assert "DEFAULT_ANSWER_MODEL" not in source  # constant fully removed
    assert "DEFAULT_ANSWER_MODEL" not in code
    assert 'os.environ.get("ANSWER_MODEL"' not in code  # no env-based default in code
    assert 'model="gpt-4o-mini"' not in code  # no literal default passed in code
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_answer_engine"
    )
    # model is REQUIRED with no default — asserted via AST (not substring) so
    # the check survives signature reflowing (R09's params broke the old
    # single-line match while the invariant held). Coordinator repair, intent
    # preserved: first keyword-only arg is bare `model`.
    assert func.args.kwonlyargs and func.args.kwonlyargs[0].arg == "model"
    assert func.args.kw_defaults[0] is None


def test_resolve_model_is_single_source_of_truth(monkeypatch):
    store = {_WS: {"routing_rules": [{"dimension": "cost", "value": "medium", "priority": 1}]}}
    monkeypatch.setenv(WORKSPACE_MODEL_CONFIG_ENV, json.dumps(store))
    model, reason = resolve_model(_WS, query_class="simple", override_model=None)
    assert (model, reason) == ("o3-mini", "workspace_rules")
