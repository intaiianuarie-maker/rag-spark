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

Precedence in route() — T-V1-028 LOCKED order (see :data:`PRECEDENCE_ORDER`):
1. sensitive veto -> local-only (never overridden). T-V1-029: the veto is a
   POLICY VERDICT, not a model name — it resolves to an explicitly allowed
   LOCAL model (:func:`configured_local_model`) or fails closed with
   :class:`LocalOnlyUnavailable`. The :data:`LOCAL_ONLY_SENTINEL` label is
   never returned as a routable model and never reaches a gateway.
2. explicit override_model wins (non-sensitive).
3. content-aware rule selection, only when routing_rules is non-empty AND
   the query class maps to a known required capability AND at least one
   registered model supports it. Otherwise fall through.
4. preferred_model (or FALLBACK_MODEL).

T-V1-029 (local-only means local or explicit unavailable):
- "local" is a two-part, explicit declaration: an explicitly allowed local
  MODEL (:data:`LOCAL_ONLY_MODEL_ENV` or a registry profile with
  ``local=True``) served over an explicitly local TRANSPORT
  (:func:`is_local_endpoint` — loopback, or a host named in
  :data:`LOCAL_GATEWAY_EXTRA_HOSTS_ENV`). A sensitive workspace with neither
  fails closed with :class:`LocalOnlyUnavailable` (documented 503 upstream);
  the sentinel label alone is never routing, and the sentinel is never posted
  to an external gateway (the provider adapter refuses it as defense in depth).

T-V1-038 discrepancy reconciled (coordinator ruling "override-above-rules
stays"): MICRO-TICKETS-V1 words the tiers *workspace > permitted override >
operator default*, but the LANDED policy orders the caller's permitted
override ABOVE the workspace rules (step 2 before step 3). That order is now
explicitly LOCKED here and pinned by test; the call site reports the tier, it
never re-orders it. "workspace" names the authoritative config SOURCE that is
LOADED (``load_workspace_config``), not a tier that outranks the override.

preferred_model is honored when: no rules are set, the query class is
unknown, or no registered model supports the required capability. Rule
values matching nothing contribute zero points but do not veto selection
among qualified models (deterministic cost/name tie-break decides).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

logger = logging.getLogger(__name__)

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
    #: T-V1-029: local-only policy veto — served by an explicitly allowed
    #: local model or failed closed, never by a cloud gateway.
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

# OpenRouter cloud model registry (zero-RAM cloud execution)
OPENROUTER_MODEL_REGISTRY: dict[str, ModelProfile] = {
    # Free tier model on OpenRouter
    "google/gemma-4-26b-a4b-it:free": ModelProfile(
        name="google/gemma-4-26b-a4b-it:free",
        capabilities=("chat", "reasoning", "multilingual"),
        cost="low",
        local=False,
        languages=("multilingual",),
    ),
    # Approved paid model
    "google/gemma-4-26b-a4b-it": ModelProfile(
        name="google/gemma-4-26b-a4b-it",
        capabilities=("chat", "reasoning", "multilingual"),
        cost="low",
        local=False,
        languages=("multilingual",),
    ),
    # Free Nemotron reranker
    "nvidia/llama-nemotron-rerank-vl-1b-v2:free": ModelProfile(
        name="nvidia/llama-nemotron-rerank-vl-1b-v2:free",
        capabilities=("rerank", "multilingual"),
        cost="low",
        local=False,
        languages=("multilingual",),
    ),
    # Legacy free reasoning model (non-China)
    "nvidia/nemotron-3.5-lightning:free": ModelProfile(
        name="nvidia/nemotron-3.5-lightning:free",
        capabilities=("chat", "reasoning"),
        cost="low",
        local=False,
        languages=("multilingual",),
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

#: T-V1-028 LOCKED precedence order, highest tier first. This tuple is the
#: single authoritative statement of precedence; :func:`route` implements it
#: and ``tests/unit/test_router_precedence_lock.py`` pins it. The caller's
#: permitted override sits ABOVE the workspace rules ("override-above-rules
#: stays" — coordinator ruling reconciling the T-V1-038 wording discrepancy).
#: Tier labels double as the ``reason`` values recorded on every Ask request.
PRECEDENCE_ORDER: tuple[str, ...] = (
    "policy_veto_local_only",  # sensitive -> local model or fail-closed (T-V1-029)
    "permitted_override",  # caller-supplied body.model (non-sensitive)
    "workspace_rules",  # authoritative per-workspace routing_rules
    "operator_default",  # operator's configured default (preferred_model/FALLBACK)
)

#: Env var carrying the authoritative per-workspace model config as a JSON
#: object keyed by workspace_id. When present and matching, its ``routing_rules``
#: feed the workspace tier so the seam actually loads config (not an empty stub).
WORKSPACE_MODEL_CONFIG_ENV = "WORKSPACE_MODEL_CONFIG"

#: Non-secret upstream transport identity for a selected model. Never includes
#: host, URL, or credentials — only the logical provider name.
PROVIDER_LITELLM = "litellm"
PROVIDER_OPENROUTER = "openrouter"
#: T-V1-029: a model served over the explicitly local transport.
PROVIDER_LOCAL = "local"

#: T-V1-029 — the policy VETO LABEL for a sensitive workspace. It is a
#: verdict, not a model name: :func:`route_decision` never returns it, and the
#: LiteLLM provider adapter refuses to post it to ANY gateway (defense in
#: depth — "sentinel label alone is not routing").
LOCAL_ONLY_SENTINEL = "local-only"

#: Operator declaration of the explicitly allowed LOCAL model for local-only
#: workspaces (e.g. an Ollama/vLLM/llama.cpp model id served over the local
#: OpenAI-compatible endpoint). Empty — the default in this tree — means no
#: local model is configured and the veto fails closed with
#: :class:`LocalOnlyUnavailable` rather than falling through to a cloud model.
LOCAL_ONLY_MODEL_ENV = "LOCAL_ONLY_MODEL"

#: Endpoint of the local model transport. Only ever used for local models;
#: never consulted for the cloud path (which keeps its own LITELLM_URL /
#: OPENROUTER_API_KEY default resolution).
LOCAL_GATEWAY_URL_ENV = "LOCAL_GATEWAY_URL"
DEFAULT_LOCAL_GATEWAY_URL = "http://127.0.0.1:4000"

#: Comma-separated EXTRA hosts an operator explicitly allows as local (e.g. a
#: LAN vLLM). Loopback is local without configuration; every other host is
#: egress until named here.
LOCAL_GATEWAY_EXTRA_HOSTS_ENV = "LOCAL_GATEWAY_EXTRA_HOSTS"

#: Hosts that count as local without further configuration.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class LocalOnlyUnavailable(RuntimeError):
    """Fail-closed verdict: a local-only request cannot be served locally.

    Raised by :func:`route_decision` when a sensitive workspace has no
    explicitly allowed local model, and by the transport seam (provider
    construction) when the configured local endpoint is not local. The message
    is a fixed, safe string — no hostname, URL, query text or credential — so
    the API layer can surface it on the 503 envelope verbatim.
    """


def _local_extra_hosts() -> frozenset[str]:
    raw = os.environ.get(LOCAL_GATEWAY_EXTRA_HOSTS_ENV, "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def is_local_endpoint(url: str) -> bool:
    """True when `url` is an EXPLICITLY local endpoint (T-V1-029).

    Local means: loopback (``localhost`` / ``127.0.0.1`` / ``::1``) or a host
    the operator named in :data:`LOCAL_GATEWAY_EXTRA_HOSTS_ENV`. Anything else
    — including any public hostname — is egress and returns ``False``. Pure
    string classification (``urllib.parse``), no DNS, no network.
    """
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return host in LOCAL_HOSTS or host in _local_extra_hosts()


def local_gateway_url() -> str:
    """Endpoint of the local model transport (env override or loopback default)."""
    return os.environ.get(LOCAL_GATEWAY_URL_ENV, "").strip() or DEFAULT_LOCAL_GATEWAY_URL


def is_local_model(model: str) -> bool:
    """True when `model` is an explicitly declared LOCAL provider identity.

    Recognised declarations (both operator-level, both visible to the policy
    AND to the transport seam, so the two can never disagree): a registry
    profile with ``local=True``, or the exact name declared in
    :data:`LOCAL_ONLY_MODEL_ENV`. Every cloud registry model and any
    undeclared name returns ``False``.
    """
    name = model.strip()
    profile = MODEL_REGISTRY.get(name)
    if profile is not None:
        return profile.local
    declared = os.environ.get(LOCAL_ONLY_MODEL_ENV, "").strip()
    return bool(declared) and declared == name


def configured_local_model() -> str | None:
    """The explicitly allowed local model for a local-only workspace, or ``None``.

    Resolution order: the operator's :data:`LOCAL_ONLY_MODEL_ENV` declaration,
    else the cheapest ``local=True`` registry profile. Returns ``None`` when
    nothing is configured — the caller (:func:`route_decision`) then fails
    closed; it never substitutes a cloud model and never returns the sentinel
    label as a model.
    """
    declared = os.environ.get(LOCAL_ONLY_MODEL_ENV, "").strip()
    if declared:
        return declared
    local = sorted(
        (p for p in MODEL_REGISTRY.values() if p.local),
        key=lambda p: (COST_RANK[p.cost], p.name),
    )
    return local[0].name if local else None


def provider_identity(model: str) -> str:
    """Non-secret upstream identity for `model` (T-V1-028 observability).

    Returns the logical provider that will serve the model — ``"local"`` for an
    explicitly declared local model (T-V1-029), ``"openrouter"`` when the model
    is in the OpenRouter registry, else ``"litellm"`` for the LiteLLM gateway.
    Deliberately reveals no host, URL, or credential: only a stable provider
    name safe to write to logs.
    """
    if is_local_model(model):
        return PROVIDER_LOCAL
    if model in OPENROUTER_MODEL_REGISTRY:
        return PROVIDER_OPENROUTER
    return PROVIDER_LITELLM


def load_workspace_config(workspace_id: str) -> WorkspaceModelConfig | None:
    """Load the authoritative per-workspace model config (T-V1-028).

    Reads the ``WORKSPACE_MODEL_CONFIG`` env var (a JSON object keyed by
    workspace_id) and returns the entry for `workspace_id`, or ``None`` when the
    var is unset, unparseable, or has no entry for this workspace. The returned
    config supplies the workspace tier (``routing_rules``) and the local-only
    policy flag (``sensitive`` — T-V1-029; a workspace opts in with
    ``"sensitive": true`` and is then served local-or-explicitly-unavailable);
    the override and operator-default tiers are layered by :func:`route` at
    request time. Malformed per-workspace entries degrade to ``None`` (no
    crash, no fabricated rules); ``sensitive`` must be the JSON boolean
    ``true`` — a string or missing value is not an opt-in.
    """
    raw = os.environ.get(WORKSPACE_MODEL_CONFIG_ENV, "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("workspace_model_config_unparseable", extra={"workspace_id": workspace_id})
        return WorkspaceModelConfig(workspace_id=workspace_id, sensitive=True)
    if not isinstance(data, dict):
        return WorkspaceModelConfig(workspace_id=workspace_id, sensitive=True)
    entry = data.get(workspace_id)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        return WorkspaceModelConfig(workspace_id=workspace_id, sensitive=True)
    rules: list[RoutingRule] = []
    for raw_rule in entry.get("routing_rules", []) or []:
        if not isinstance(raw_rule, dict):
            continue
        try:
            rules.append(
                RoutingRule(
                    dimension=RoutingDimension(raw_rule["dimension"]),
                    value=str(raw_rule["value"]),
                    priority=int(raw_rule.get("priority", 0)),
                )
            )
        except (KeyError, ValueError):
            continue  # skip a malformed rule, keep the valid ones
    return WorkspaceModelConfig(
        workspace_id=workspace_id,
        routing_rules=tuple(rules),
        sensitive=entry.get("sensitive") is True,
    )


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
    """Deterministic router implementing the T-V1-028 LOCKED precedence order.

    Order (see :data:`PRECEDENCE_ORDER`): veto -> permitted override -> workspace
    rules -> operator default. The caller reports the tier via
    :func:`route_decision`; it never re-orders or re-derives it. A sensitive
    workspace with no configured local model raises
    :class:`LocalOnlyUnavailable` (T-V1-029 fail-closed) instead of returning
    the sentinel label or any cloud model.
    """
    return route_decision(query_class, config)[0]


def route_decision(query_class: str, config: WorkspaceModelConfig) -> tuple[str, str]:
    """Resolve the model AND the precedence-tier reason in one policy pass.

    Returns ``(model, reason)`` where `reason` names the tier that produced the
    model: ``policy_veto_local_only``, ``permitted_override``,
    ``workspace_rules``, ``operator_default`` (operator configured a default), or
    ``operator_default_unconfigured`` (no default configured — the policy's own
    :data:`FALLBACK_MODEL` applied). The first four map onto
    :data:`PRECEDENCE_ORDER`; the last is a sub-case of ``operator_default``
    kept distinct so "no default configured" stays observable. Deriving the
    reason here — inside the policy — makes the recorded reason and the selected
    model inseparable, so a call site can never log a reason that disagrees with
    the actual selection.

    T-V1-029 — the veto tier resolves, it does not label: a sensitive
    workspace gets the explicitly allowed local model
    (:func:`configured_local_model`), or :class:`LocalOnlyUnavailable` when
    none is configured. The ``local-only`` sentinel is NEVER returned as a
    model, so no call site can post it to a gateway.
    """
    if config.sensitive:
        local = configured_local_model()
        if local is None:
            raise LocalOnlyUnavailable(
                "no local model is configured for this local-only workspace"
            )
        return local, "policy_veto_local_only"
    if config.override_model is not None:
        return config.override_model, "permitted_override"
    if config.routing_rules:
        selected = _select_by_rules(query_class, config)
        if selected is not None:
            return selected, "workspace_rules"
    if config.preferred_model is not None:
        return config.preferred_model, "operator_default"
    return FALLBACK_MODEL, "operator_default_unconfigured"
