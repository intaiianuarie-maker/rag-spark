"""Model router + capability/cost metadata registry (TASK-M7-001a).

Reuse: none new (LiteLLM MIT adopted M4-001; see rag-fork-solution REUSE_MAP.md).
Build: per-model capability/cost metadata registry + content-aware router + veto.

M7-001 arbitration (DECISIONS.md M7-001b condition 3) context:
- M7-001b shipped a hardcoded capability_map that silently BYPASSED
  per-workspace preferred_model and ignored routing_rules entirely, so
  DEFAULT vs no-rules vs explicit rules were provably identical.
- Round-1 M7-001a gated on rule PRESENCE only (rule contents uninspected).
- Round-2 (this file) inspects rule CONTENTS: every rule dimension/value
  contributes per-model score points, rule priority sets the weight, and
  the query class maps to a REQUIRED capability candidates must support.
  Different rule sets provably select different models.

Precedence in route():
1. sensitive veto -> "local-only" (never overridden).
2. explicit override_model wins (non-sensitive).
3. content-aware rule selection, only when routing_rules is non-empty AND
   the query class maps to a known required capability AND at least one
   registered model supports it. Otherwise fall through.
4. preferred_model (or FALLBACK_MODEL).

preferred_model is honored when: no rules are set, the query class is
unknown, or no registered model supports the required capability. Rule
values matching nothing contribute zero points but do not veto selection
among qualified models (deterministic cost/name tie-break decides).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

CostTier = Literal["low", "medium", "high"]


class RoutingDimension(StrEnum):
    CAPABILITY = "capability"
    COST = "cost"
    LOCAL_CLOUD = "local_cloud"
    LANGUAGE = "language"


@dataclass(frozen=True)
class RoutingRule:
    dimension: RoutingDimension
    value: str
    priority: int = 0


@dataclass(frozen=True)
class ModelProfile:
    """Capability/cost metadata for one routable model."""

    name: str
    capabilities: tuple[str, ...] = ("chat",)
    cost: CostTier = "low"
    local: bool = False
    languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceModelConfig:
    workspace_id: str
    preferred_model: str | None = None
    override_model: str | None = None
    routing_rules: tuple[RoutingRule, ...] = ()
    rate_limit_policy: Literal["fail_open", "fail_closed"] = "fail_closed"
    verify_uniform_cost: bool = True
    extra_models: tuple[ModelProfile, ...] = ()
    sensitive: bool = False


# Frozen tuple (immutable container).
DEFAULT_ROUTING_RULES: tuple[RoutingRule, ...] = (
    RoutingRule(RoutingDimension.CAPABILITY, "chat", priority=1),
    RoutingRule(RoutingDimension.COST, "low", priority=2),
    RoutingRule(RoutingDimension.LOCAL_CLOUD, "local", priority=3),
    RoutingRule(RoutingDimension.LANGUAGE, "multilingual", priority=4),
)

# M7-001a capability/cost metadata registry (module default).
MODEL_REGISTRY: dict[str, ModelProfile] = {
    "gpt-4o-mini": ModelProfile(
        name="gpt-4o-mini",
        capabilities=("chat", "multilingual"),
        cost="low",
        local=False,
        languages=("multilingual",),
    ),
    "o3-mini": ModelProfile(
        name="o3-mini",
        capabilities=("chat", "reasoning"),
        cost="medium",
        local=False,
        languages=(),
    ),
    "o1-mini": ModelProfile(
        name="o1-mini",
        capabilities=("chat", "code", "reasoning"),
        cost="high",
        local=False,
        languages=(),
    ),
}

# Query class -> capability a selected model MUST support. Unknown classes
# map to nothing, which routes to preferred_model (test-enshrined).
REQUIRED_CAPABILITY: dict[str, str] = {
    "simple": "chat",
    "complex": "reasoning",
    "code": "code",
    "multilingual": "multilingual",
}

COST_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

FALLBACK_MODEL = "gpt-4o-mini"


def _rule_matches(rule: RoutingRule, profile: ModelProfile) -> bool:
    """True when a model's metadata satisfies one rule's dimension/value."""
    if rule.dimension is RoutingDimension.CAPABILITY:
        return rule.value in profile.capabilities
    if rule.dimension is RoutingDimension.COST:
        return rule.value == profile.cost
    if rule.dimension is RoutingDimension.LOCAL_CLOUD:
        if rule.value == "local":
            return profile.local
        if rule.value == "cloud":
            return not profile.local
        return False
    return rule.value in profile.languages


def _candidates(config: WorkspaceModelConfig) -> dict[str, ModelProfile]:
    """Module registry merged with per-workspace extra models."""
    merged: dict[str, ModelProfile] = dict(MODEL_REGISTRY)
    for extra in config.extra_models:
        merged[extra.name] = extra
    return merged


def _select_by_rules(query_class: str, config: WorkspaceModelConfig) -> str | None:
    """Content-aware selection. Returns None when rules cannot select."""
    required = REQUIRED_CAPABILITY.get(query_class)
    if required is None:
        return None
    qualified = [p for p in _candidates(config).values() if required in p.capabilities]
    if not qualified:
        return None
    rules = config.routing_rules
    if not rules:
        return None
    top_priority = max(r.priority for r in rules)

    def score(profile: ModelProfile) -> int:
        total = 0
        for rule in rules:
            if _rule_matches(rule, profile):
                total += top_priority - rule.priority + 1
        return total

    best = min(qualified, key=lambda p: (-score(p), COST_RANK[p.cost], p.name))
    return best.name


def route(query_class: str, config: WorkspaceModelConfig) -> str:
    """Deterministic router: veto -> override -> content-aware rules -> preferred."""
    if config.sensitive:
        return "local-only"
    if config.override_model is not None:
        return config.override_model
    if config.routing_rules:
        selected = _select_by_rules(query_class, config)
        if selected is not None:
            return selected
    return config.preferred_model or FALLBACK_MODEL
