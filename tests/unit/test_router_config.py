"""Capability/cost registry router tests — TASK-M7-001a round-2.

Acceptance mapping (TASKS.md M7-001a + DECISIONS.md M7-001b condition 3):
- routing rules select among models by capability/cost dimensions from
  workspace metadata (MODEL_REGISTRY default + per-workspace extra_models).
- rule CONTENTS (dimension/value/priority) decide the outcome: different
  rule sets provably select different models.
- preferred_model honored when no rule matches (no rules, unknown query
  class, or no registered model supports the required capability).
- rules-vs-no-rules provably different in the ko-venv suite.
- sensitive veto: resolves to an explicitly allowed local model or fails
  closed with LocalOnlyUnavailable — never the `local-only` label as a model
  name (T-V1-029).
"""

import pytest
from models.router_config import (
    DEFAULT_ROUTING_RULES,
    LOCAL_ONLY_MODEL_ENV,
    MODEL_REGISTRY,
    REQUIRED_CAPABILITY,
    LocalOnlyUnavailable,
    ModelProfile,
    RoutingDimension,
    RoutingRule,
    WorkspaceModelConfig,
    route,
)


def _with_rules(**kwargs):
    base = {"workspace_id": "ws-1", "routing_rules": DEFAULT_ROUTING_RULES}
    base.update(kwargs)
    return WorkspaceModelConfig(**base)


def test_veto_fails_closed_when_no_local_model_is_configured():
    """T-V1-029 re-pin: the veto is local-or-explicit-unavailable, never a label.

    The pre-T-V1-029 contract returned the ``local-only`` SENTINEL as if it
    were a routable model name — which the provider would then post to
    whatever gateway was configured (possibly an external one). With no
    explicitly allowed local model configured, every query class on a
    sensitive workspace now raises :class:`LocalOnlyUnavailable` (documented
    503 upstream) instead.
    """
    config = _with_rules(
        preferred_model="gpt-4o-mini",
        override_model="o3-mini",
        sensitive=True,
    )
    for query_class in ("complex", "simple", "multilingual"):
        with pytest.raises(LocalOnlyUnavailable):
            route(query_class, config)


def test_veto_resolves_to_the_configured_local_model(monkeypatch):
    """T-V1-029: an explicitly allowed local model beats override, preferred
    and rules on a sensitive workspace — and it is a real model, not the label."""
    monkeypatch.setenv(LOCAL_ONLY_MODEL_ENV, "llama3.1:8b")
    config = _with_rules(
        preferred_model="gpt-4o-mini",
        override_model="o3-mini",
        sensitive=True,
    )
    for query_class in ("complex", "simple", "multilingual"):
        assert route(query_class, config) == "llama3.1:8b"


def test_simple_fast():
    assert route("simple", _with_rules()) == "gpt-4o-mini"


def test_complex_reasoning():
    assert route("complex", _with_rules()) == "o3-mini"


def test_code_coding():
    assert route("code", _with_rules()) == "o1-mini"


def test_multilingual():
    assert route("multilingual", _with_rules()) == "gpt-4o-mini"


def test_override_wins_except_veto():
    config = _with_rules(
        preferred_model="gpt-4o-mini",
        override_model="o3-mini",
    )
    assert route("simple", config) == "o3-mini"
    assert route("complex", config) == "o3-mini"


def test_default_tuple_immutable():
    assert isinstance(DEFAULT_ROUTING_RULES, tuple)
    assert all(isinstance(r, RoutingRule) for r in DEFAULT_ROUTING_RULES)


def test_rules_vs_no_rules_provably_different():
    with_rules = WorkspaceModelConfig(
        workspace_id="ws-diff",
        preferred_model="my-custom-model",
        routing_rules=DEFAULT_ROUTING_RULES,
    )
    no_rules = WorkspaceModelConfig(
        workspace_id="ws-diff",
        preferred_model="my-custom-model",
    )
    assert route("code", with_rules) == MODEL_REGISTRY["o1-mini"].name
    assert route("code", no_rules) == "my-custom-model"


def test_preferred_honored_when_no_rule_matches():
    config = _with_rules(preferred_model="my-preferred")
    assert route("unknown-class-xyz", config) == "my-preferred"


def test_preferred_honored_when_no_rules_and_no_preferred():
    config = WorkspaceModelConfig(workspace_id="ws-bare")
    assert route("code", config) == "gpt-4o-mini"
    assert route("unknown-class-xyz", config) == "gpt-4o-mini"


def test_rule_contents_change_outcome():
    default_pick = route("complex", _with_rules())
    assert default_pick == "o3-mini"
    cost_high = WorkspaceModelConfig(
        workspace_id="ws-1",
        routing_rules=(RoutingRule(RoutingDimension.COST, "high", priority=1),),
    )
    assert route("complex", cost_high) == "o1-mini"
    assert route("complex", cost_high) != default_pick


def test_capability_rule_steers_selection():
    config = WorkspaceModelConfig(
        workspace_id="ws-1",
        routing_rules=(RoutingRule(RoutingDimension.CAPABILITY, "code", priority=1),),
    )
    assert route("complex", config) == "o1-mini"


def test_priority_orders_rules():
    med_first = WorkspaceModelConfig(
        workspace_id="ws-1",
        routing_rules=(
            RoutingRule(RoutingDimension.COST, "medium", priority=1),
            RoutingRule(RoutingDimension.COST, "high", priority=2),
        ),
    )
    high_first = WorkspaceModelConfig(
        workspace_id="ws-1",
        routing_rules=(
            RoutingRule(RoutingDimension.COST, "high", priority=1),
            RoutingRule(RoutingDimension.COST, "medium", priority=2),
        ),
    )
    assert route("complex", med_first) == "o3-mini"
    assert route("complex", high_first) == "o1-mini"


def test_nonmatching_rule_values_fall_back_to_cheapest_qualified():
    config = WorkspaceModelConfig(
        workspace_id="ws-1",
        routing_rules=(RoutingRule(RoutingDimension.COST, "ultra", priority=1),),
    )
    assert route("complex", config) == "o3-mini"


def test_local_cloud_rules_honored():
    cloud = WorkspaceModelConfig(
        workspace_id="ws-1",
        routing_rules=(RoutingRule(RoutingDimension.LOCAL_CLOUD, "cloud", priority=1),),
    )
    local = WorkspaceModelConfig(
        workspace_id="ws-1",
        routing_rules=(RoutingRule(RoutingDimension.LOCAL_CLOUD, "local", priority=1),),
    )
    assert route("complex", cloud) == "o3-mini"
    assert route("complex", local) == "o3-mini"


def test_extra_models_workspace_metadata():
    cheap_reasoner = ModelProfile(
        name="ws-cheap-reasoner",
        capabilities=("chat", "reasoning"),
        cost="low",
        local=False,
        languages=(),
    )
    config = WorkspaceModelConfig(
        workspace_id="ws-1",
        preferred_model="my-preferred",
        routing_rules=DEFAULT_ROUTING_RULES,
        extra_models=(cheap_reasoner,),
    )
    assert route("complex", config) == "ws-cheap-reasoner"
    no_rules = WorkspaceModelConfig(
        workspace_id="ws-1",
        preferred_model="my-preferred",
        extra_models=(cheap_reasoner,),
    )
    assert route("complex", no_rules) == "my-preferred"


def test_registry_documents_required_capabilities():
    assert set(REQUIRED_CAPABILITY) == {"simple", "complex", "code", "multilingual"}
    assert all(len(p.capabilities) > 0 for p in MODEL_REGISTRY.values())
    for _query_class, required in REQUIRED_CAPABILITY.items():
        assert any(required in p.capabilities for p in MODEL_REGISTRY.values())
