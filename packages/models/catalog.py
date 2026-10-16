"""Catalogue admission & policy enforcement (T-OR-01).

:data:`ADMITTED_MODELS` is the single source of truth for which models may be
routed through OpenRouter for each pipeline role. Two independent guards run at
admission:

1. **Non-China lineage guard** — the base-model publisher and the serving
   provider must originate from an approved jurisdiction. Restricted-jurisdiction
   lineages/providers are rejected (see :data:`RESTRICTED_LINEAGE_PATTERNS`,
   :data:`RESTRICTED_PROVIDER_TAGS`), mirroring
   ``scripts/model_origin_guard.sh`` (PD-005 v3) but enforced *in-process* so a
   mis-built catalogue can never silently widen a route.

2. **Provider allowlist builder** (:func:`build_provider_only`) — every route
   carries a non-empty ``allowed_providers`` list backing OpenRouter
   ``provider.only``. An empty list is **fail-closed** (no unrestricted routing).

Resolution (:func:`resolve_inference_request`) applies the fail-closed data-class
rules from :mod:`packages.models.policy`:

* ``local_only``               -> :class:`EgressPolicyViolation` (no remote egress).
* ``cloud_approved_sensitive`` -> rejects free endpoints that forbid confidential
  data (e.g. the NVIDIA free reranker); falls through to the approved paid
  fallback when one exists, otherwise :class:`SensitiveRouteUnavailable`.
* ``public``                   -> free tier preferred, paid fallback on pool
  exhaustion (free-first) or directly (paid-only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .policy import (
    Capability,
    ChinaOriginRejected,
    DataClass,
    DataCollectionPolicy,
    EgressPolicyViolation,
    EmptyProviderAllowlist,
    Lineage,
    Modality,
    ModelRole,
    ModelRoute,
    PolicyError,
    ProviderPolicy,
    ResolvedInferenceRequest,
    SensitiveRouteUnavailable,
    Tier,
)

__all__ = [
    "ADMITTED_MODELS",
    "APPROVED_PUBLISHERS",
    "RESTRICTED_LINEAGE_PATTERNS",
    "RESTRICTED_PROVIDER_TAGS",
    "AdmittedRoute",
    "RouteTier",
    "admit_model_route",
    "assert_nonchina_lineage",
    "build_provider_only",
    "is_china_origin",
    "resolve_inference_request",
    "validate_catalog",
]


# ---------------------------------------------------------------------------
# Non-China origin guard
# ---------------------------------------------------------------------------
#
# A denylist of China-origin publishers / provider tags, enforced on every
# admission path. It is NOT a model allowlist — admission additionally requires
# an explicit, non-empty provider allowlist (see build_provider_only). Mirrors
# ``scripts/model_origin_guard.sh`` so the shell gate and the in-process guard
# cannot drift.

#: Model base-model / publisher substrings flagged as China-origin / restricted.
RESTRICTED_LINEAGE_PATTERNS: tuple[str, ...] = (
    "bge-reranker-v2-m3",  # BAAI cross-encoder reranker family (China)
    "moonshot",  # Moonshot (China)
    "deepseek",  # DeepSeek (China)
    "zhipuai",  # ZhipuAI (China)
    "tencent",  # Tencent (China)
    "aliyun",  # Alibaba Cloud / Aliyun (China)
    "01-ai",  # 01.ai / Jiuding / K2 (China)
)

#: OpenRouter provider tags whose jurisdiction is restricted.
RESTRICTED_PROVIDER_TAGS: tuple[str, ...] = (
    "moonshot",
    "deepseek",
    "zhipuai",
    "aliyun",
    "tencent",
)

#: Approved model-family publishers (non-China). Reference set; the active guard
#: is the denylist above so a new benign publisher is admitted unless restricted.
APPROVED_PUBLISHERS: frozenset[str] = frozenset(
    {"google", "nvidia", "cohere", "sentence-transformers", "cross-encoder"}
)

#: Static authn envelope the transport stamps (Authorization added by the
#: transport/adapter; never a secret kept here).
_OPENROUTER_HEADERS: dict[str, str] = {
    "content-type": "application/json",
    "http-referer": "https://knowledge-os.local",
    "x-title": "knowledge-os",
}


def is_china_origin(lineage: Lineage, provider: str) -> bool:
    """True if the base-model lineage OR the serving provider is restricted."""
    haystack = f"{lineage.base_model} {lineage.publisher}".lower()
    for pattern in RESTRICTED_LINEAGE_PATTERNS:
        if pattern in haystack:
            return True
    provider_l = provider.lower()
    for tag in RESTRICTED_PROVIDER_TAGS:
        if tag in provider_l:
            return True
    return False


def assert_nonchina_lineage(route: ModelRoute, provider: str) -> None:
    """Guard 1: reject restricted-jurisdiction lineage/providers at admission."""
    if is_china_origin(route.lineage, provider):
        raise ChinaOriginRejected(
            "route rejected: restricted origin "
            f"(model={route.model}, base={route.lineage.base_model}, "
            f"publisher={route.lineage.publisher}, provider={provider})"
        )


# ---------------------------------------------------------------------------
# Provider allowlist builder
# ---------------------------------------------------------------------------


def build_provider_only(policy: ProviderPolicy) -> tuple[str, ...]:
    """Build the non-empty ``provider.only`` allowlist (fail-closed if empty).

    OpenRouter routes to a provider only when its tag appears in
    ``provider.only``; omitting the field means *any* provider may serve the
    model. We never emit an unrestricted route — an empty allowlist raises
    :class:`EmptyProviderAllowlist` instead.
    """
    providers = policy.allowed_providers
    if not providers:
        raise EmptyProviderAllowlist(
            "provider.only is empty: fail-closed (no unrestricted routing)"
        )
    return providers


# ---------------------------------------------------------------------------
# Admitted route types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteTier:
    """One billing tier of an admitted route (free or paid).

    ``confidential_ok`` records whether this tier's endpoints are approved to
    receive ``cloud_approved_sensitive`` data. The NVIDIA free reranker, for
    example, advertises public-data-only and therefore sets it ``False``.
    ``constraints`` carries tier-scoped endpoint parameters the transport may
    honour (e.g. embedding batch limits / dimensions).
    """

    model: str
    policy: ProviderPolicy
    confidential_ok: bool
    constraints: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class AdmittedRoute:
    """An admitted model across its free and/or paid tiers.

    The primary tier (``free`` for free-first, ``paid`` for paid-only) is the
    one used for ``public`` data. Both are consulted when resolving
    ``cloud_approved_sensitive`` data so the free endpoint can be rejected in
    favour of a confidential-approved paid fallback.
    """

    route: ModelRoute
    free: RouteTier | None = None
    paid: RouteTier | None = None

    def tiers(self) -> tuple[RouteTier, ...]:
        """All admitted tiers (free then paid), omitting absent ones."""
        return tuple(t for t in (self.free, self.paid) if t is not None)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def admit_model_route(
    route: ModelRoute,
    *,
    free: RouteTier | None = None,
    paid: RouteTier | None = None,
) -> AdmittedRoute:
    """Admit a model route, enforcing the allowlist + non-China guards.

    Raises:
        EmptyProviderAllowlist:
            No tier was supplied, or a tier's ``allowed_providers`` is empty.
        ChinaOriginRejected:
            A base-model lineage or provider is from a restricted jurisdiction.
    """
    if free is None and paid is None:
        raise EmptyProviderAllowlist(
            f"no tier admitted for model={route.model} (role={route.role}): "
            "a route must carry at least one non-empty provider allowlist"
        )
    for tier in (free, paid):
        if tier is None:
            continue
        # Fail-closed: provider.only must be a non-empty allowlist.
        build_provider_only(tier.policy)
        # Non-China lineage guard over every admitted provider.
        for provider in tier.policy.allowed_providers:
            assert_nonchina_lineage(route, provider)
    return AdmittedRoute(route=route, free=free, paid=paid)


# ---------------------------------------------------------------------------
# Catalogue (single source of truth)
# ---------------------------------------------------------------------------
#
# Every entry is built through :func:`admit_model_route`, so the fail-closed
# guards (non-empty ``provider.only`` + non-China origin) execute at import
# time. A mis-built route raises at module load rather than silently widening
# a route downstream.


def _tier(
    model: str,
    providers: tuple[str, ...],
    *,
    confidential_ok: bool = True,
    regions: tuple[str, ...] = (),
    constraints: dict[str, Any] | None = None,
) -> RouteTier:
    """Construct an admitted :class:`RouteTier` with a deny-by-default policy."""
    return RouteTier(
        model=model,
        policy=ProviderPolicy(
            allowed_providers=providers,
            data_collection=DataCollectionPolicy.DENY,
            regions=regions,
        ),
        confidential_ok=confidential_ok,
        constraints=dict(constraints or {}),
    )


#: ``(role, model_slug) -> AdmittedRoute``. The authoritative set of models
#: routable through OpenRouter. Add a model here — never bypass admission.
ADMITTED_MODELS: dict[tuple[ModelRole, str], AdmittedRoute] = {
    # Embedding ------------------------------------------------------------
    # ``google/gemini-embedding-2`` (3072-d). Paid-only on OpenRouter — no free
    # tier exists — so there is no paid-fallback escalation for this role.
    (ModelRole.EMBEDDING, "google/gemini-embedding-2"): admit_model_route(
        ModelRoute(
            model="google/gemini-embedding-2",
            role=ModelRole.EMBEDDING,
            permitted_tiers=(Tier.PAID_ONLY,),
            capability=Capability(modalities=(Modality.TEXT,)),
            lineage=Lineage(base_model="gemini-embedding-2", publisher="google"),
        ),
        paid=_tier(
            "google/gemini-embedding-2",
            ("google-ai-studio", "google-vertex"),
            confidential_ok=True,
        ),
    ),
    # Generation -----------------------------------------------------------
    # ``google/gemma-4-26b-a4b-it``: free ``:free`` slug via Google AI Studio
    # first, paid fallback on exhaustion (free-first) via CoreWeave.
    (ModelRole.GENERATION, "google/gemma-4-26b-a4b-it"): admit_model_route(
        ModelRoute(
            model="google/gemma-4-26b-a4b-it",
            role=ModelRole.GENERATION,
            permitted_tiers=(Tier.FREE_FIRST,),
            capability=Capability(modalities=(Modality.TEXT,), tools=True),
            lineage=Lineage(base_model="gemma-4-26b-a4b-it", publisher="google"),
        ),
        free=_tier(
            "google/gemma-4-26b-a4b-it:free",
            ("google-ai-studio",),
            confidential_ok=True,
        ),
        paid=_tier(
            "google/gemma-4-26b-a4b-it",
            ("coreweave",),
            confidential_ok=True,
        ),
    ),
    # Rerank ---------------------------------------------------------------
    # ``nvidia/llama-nemotron-rerank-vl-1b-v2:free``: the *free* NVIDIA reranker
    # advertises public-data-only (``confidential_ok=False``). It is the only
    # admitted rerank tier — there is no Cohere/paid fallback — so sensitive
    # rerank data has no confidentiality-approved endpoint and is rejected with
    # :class:`SensitiveRouteUnavailable`.
    (ModelRole.RERANK, "nvidia/llama-nemotron-rerank-vl-1b-v2:free"): admit_model_route(
        ModelRoute(
            model="nvidia/llama-nemotron-rerank-vl-1b-v2:free",
            role=ModelRole.RERANK,
            permitted_tiers=(Tier.FREE_FIRST,),
            capability=Capability(modalities=(Modality.TEXT,)),
            lineage=Lineage(base_model="llama-nemotron-rerank-vl-1b-v2", publisher="nvidia"),
        ),
        free=_tier(
            "nvidia/llama-nemotron-rerank-vl-1b-v2:free",
            ("nvidia",),
            confidential_ok=False,
        ),
    ),
}


# ---------------------------------------------------------------------------
# Catalogue validation
# ---------------------------------------------------------------------------


def validate_catalog() -> dict[tuple[ModelRole, str], AdmittedRoute]:
    """Defensive fail-closed check of the live :data:`ADMITTED_MODELS` map.

    Re-runs both admission guards over every provider of every admitted tier so a
    hand-edited catalogue can never silently widen a route. Safe to call at
    import time (``from models.catalog import validate_catalog``) or from tests.

    Raises:
        EmptyProviderAllowlist: the catalogue is empty or a tier lacks providers.
        ChinaOriginRejected:    a restricted lineage/provider slipped in.
    """
    if not ADMITTED_MODELS:
        raise EmptyProviderAllowlist("catalogue is empty: no admitted routes")
    for (role, model), admitted in ADMITTED_MODELS.items():
        if not admitted.tiers():
            raise EmptyProviderAllowlist(f"admitted route has no tier (role={role}, model={model})")
        for tier in admitted.tiers():
            build_provider_only(tier.policy)
            for provider in tier.policy.allowed_providers:
                assert_nonchina_lineage(admitted.route, provider)
    return ADMITTED_MODELS


# ---------------------------------------------------------------------------
# Inference resolution
# ---------------------------------------------------------------------------


def _lookup_admitted(role: ModelRole, model: str | None) -> AdmittedRoute:
    """Resolve a ``(role, model)`` to an admitted route.

    ``model=None`` selects the first admitted route for ``role``; an unknown
    ``(role, model)`` is a policy error, never a silent default.
    """
    if model is None:
        for key, route in ADMITTED_MODELS.items():
            if key[0] is role:
                return route
        raise PolicyError(f"no admitted route for role={role}")
    try:
        return ADMITTED_MODELS[(role, model)]
    except KeyError as exc:
        raise PolicyError(f"model {model!r} is not admitted for role={role}") from exc


def _choose_tier(
    route: AdmittedRoute,
    data_class: DataClass,
) -> tuple[RouteTier, RouteTier | None]:
    """Select the primary tier and an optional fallback tier.

    Returns ``(primary, fallback)`` where ``fallback`` is the secondary tier
    OpenRouter may use on provider pool exhaustion (``allow_fallbacks=True``),
    or ``None`` when no fallback tier is admitted.

    * ``public``                   -> free preferred, paid fallback on exhaustion.
    * ``cloud_approved_sensitive`` -> only ``confidential_ok`` tiers are
      candidates; the free reranker that forbids confidential data is rejected
      and we fall through to the approved paid tier.
    * ``local_only``               -> vetoed by the caller before this runs.
    """
    free, paid = route.free, route.paid
    if data_class is DataClass.CLOUD_APPROVED_SENSITIVE:
        candidates = [t for t in (free, paid) if t is not None and t.confidential_ok]
        if not candidates:
            raise SensitiveRouteUnavailable(
                "no confidential-approved tier for sensitive data "
                f"(role={route.route.role}, model={route.route.model})"
            )
        return candidates[0], candidates[1] if len(candidates) > 1 else None
    # DataClass.PUBLIC
    primary = free if free is not None else paid
    if primary is None:
        raise SensitiveRouteUnavailable(
            f"no tier admitted for route (role={route.route.role}, model={route.route.model})"
        )
    fallback = paid if (primary is free and paid is not None) else None
    return primary, fallback


def resolve_inference_request(
    *,
    role: ModelRole,
    data_class: DataClass = DataClass.PUBLIC,
    model: str | None = None,
) -> ResolvedInferenceRequest:
    """Resolve a request to a concrete :class:`ResolvedInferenceRequest`.

    Applies the fail-closed data-class rules:

    * ``local_only``               -> :class:`EgressPolicyViolation`.
    * ``cloud_approved_sensitive`` -> free endpoints that forbid confidential
      data are rejected; the approved paid fallback is used when available,
      else :class:`SensitiveRouteUnavailable`.
    * ``public``                   -> free tier preferred with a paid fallback
      on pool exhaustion (free-first) or the paid tier directly (paid-only).

    The returned ``provider.only`` is the union of the primary tier's and
    fallback tier's providers (primary first); ``allow_fallbacks`` is ``True``
    only when a fallback tier is admitted, so an exhausted pool can escalate
    without widening beyond the admitted allowlist.
    """
    if data_class is DataClass.LOCAL_ONLY:
        raise EgressPolicyViolation(
            f"local_only data must not be routed to a remote provider "
            f"(role={role}, model={model!r})"
        )
    route = _lookup_admitted(role, model)
    primary, fallback = _choose_tier(route, data_class)
    providers: list[str] = [*primary.policy.allowed_providers]
    allow_fallbacks = False
    if fallback is not None:
        for provider in fallback.policy.allowed_providers:
            if provider not in providers:
                providers.append(provider)
        allow_fallbacks = True
    params: dict[str, Any] = {
        "provider": {"only": providers},
        "allow_fallbacks": allow_fallbacks,
    }
    if primary.policy.data_collection is DataCollectionPolicy.DENY:
        params["provider"]["data_collection"] = "none"
    return ResolvedInferenceRequest(
        model=primary.model,
        provider=providers[0],
        params=params,
        headers=dict(_OPENROUTER_HEADERS),
    )
