"""T-OR-01 — catalogue admission & policy enforcement tests."""

from __future__ import annotations

import pytest
from models.catalog import (
    ADMITTED_MODELS,
    AdmittedRoute,
    RouteTier,
    admit_model_route,
    build_provider_only,
    is_china_origin,
    resolve_inference_request,
    validate_catalog,
)
from models.policy import (
    ChinaOriginRejected,
    DataClass,
    EgressPolicyViolation,
    EmptyProviderAllowlist,
    Lineage,
    ModelRole,
    ModelRoute,
    PolicyError,
    ProviderPolicy,
    SensitiveRouteUnavailable,
    Tier,
)

pytestmark = pytest.mark.unit

EMB = "google/gemini-embedding-2"
GEN = "google/gemma-4-26b-a4b-it"
RER = "nvidia/llama-nemotron-rerank-vl-1b-v2:free"


def _route(model: str, role: ModelRole, pub: str, tier: Tier = Tier.FREE_FIRST) -> ModelRoute:
    return ModelRoute(
        model=model,
        role=role,
        permitted_tiers=(tier,),
        lineage=Lineage(base_model=model.split("/")[-1], publisher=pub),
    )


def _tier(model: str, providers: tuple[str, ...], confidential_ok: bool = True) -> RouteTier:
    return RouteTier(model, ProviderPolicy(allowed_providers=providers), confidential_ok)


# --- Admitted catalogue ---------------------------------------------------


@pytest.mark.parametrize(
    "role,slug",
    [
        (ModelRole.EMBEDDING, EMB),
        (ModelRole.GENERATION, GEN),
        (ModelRole.SEMANTIC_VERIFICATION, GEN),
        (ModelRole.RERANK, RER),
        (ModelRole.GRAPH, GEN),
        (ModelRole.AGENT, GEN),
        (ModelRole.WORKFLOW, GEN),
    ],
)
def test_routes_admitted(role: ModelRole, slug: str) -> None:
    route = ADMITTED_MODELS[(role, slug)]
    assert isinstance(route, AdmittedRoute)
    assert route.tiers()


def test_catalogue_validation_is_identity() -> None:
    assert validate_catalog() is ADMITTED_MODELS


def test_embedding_is_paid_only_google_providers() -> None:
    r = ADMITTED_MODELS[(ModelRole.EMBEDDING, EMB)]
    assert r.free is None and r.paid is not None
    assert r.paid.policy.allowed_providers == ("google-ai-studio", "google-vertex")
    assert r.paid.confidential_ok is True


def test_generation_is_free_first_with_coreweave_paid() -> None:
    r = ADMITTED_MODELS[(ModelRole.GENERATION, GEN)]
    assert r.free is not None and r.free.model == f"{GEN}:free"
    assert r.free.policy.allowed_providers == ("google-ai-studio",)
    assert r.paid is not None and r.paid.model == GEN
    assert r.paid.policy.allowed_providers == ("coreweave",)


def test_rerank_is_free_only_nvidia_no_cohere() -> None:
    r = ADMITTED_MODELS[(ModelRole.RERANK, RER)]
    assert r.free is not None
    assert r.free.policy.allowed_providers == ("nvidia",)
    assert r.free.confidential_ok is False
    assert r.paid is None
    allowed = {p for t in r.tiers() for p in t.policy.allowed_providers}
    assert allowed == {"nvidia"}  # Cohere removed completely


# --- Non-China lineage guard ---------------------------------------------


@pytest.mark.parametrize(
    "base,pub,prov",
    [
        ("bge-reranker-v2-m3", "BAAI", "google"),  # restricted lineage
        ("deepseek-chat", "deepseek", "google"),  # restricted publisher
        ("k2", "01-ai", "google"),
        ("gemini-2", "google", "moonshot"),  # clean lineage, restricted provider
        ("gemini-2", "google", "aliyun"),
    ],
)
def test_is_china_origin_flags(base: str, pub: str, prov: str) -> None:
    assert is_china_origin(Lineage(base_model=base, publisher=pub), prov)


def test_is_china_origin_clean() -> None:
    assert not is_china_origin(Lineage("gemini-2", "google"), "google-ai-studio")
    assert not is_china_origin(Lineage("nemotron", "nvidia"), "nvidia")


def test_admit_rejects_china_lineage() -> None:
    route = _route("deepseek/deepseek-chat", ModelRole.GENERATION, "deepseek", Tier.PAID_ONLY)
    with pytest.raises(ChinaOriginRejected):
        admit_model_route(route, paid=_tier("deepseek/deepseek-chat", ("deepseek",)))


def test_admit_rejects_restricted_provider() -> None:
    route = _route("google/gemini-2", ModelRole.GENERATION, "google")
    with pytest.raises(ChinaOriginRejected):
        admit_model_route(route, free=_tier("google/gemini-2", ("deepseek",)))


# --- Empty allowlist is fail-closed --------------------------------------


def test_empty_provider_allowlist_rejected() -> None:
    with pytest.raises(EmptyProviderAllowlist):
        build_provider_only(ProviderPolicy(allowed_providers=()))


def test_admit_rejects_empty_allowlist() -> None:
    route = _route("google/gemini-2", ModelRole.GENERATION, "google")
    with pytest.raises(EmptyProviderAllowlist):
        admit_model_route(route, free=_tier("google/gemini-2", ()))


def test_admit_rejects_no_tier() -> None:
    with pytest.raises(EmptyProviderAllowlist):
        admit_model_route(_route("google/gemini-2", ModelRole.GENERATION, "google"))


# --- local_only egress veto ----------------------------------------------


@pytest.mark.parametrize("role", [ModelRole.EMBEDDING, ModelRole.GENERATION, ModelRole.RERANK])
def test_local_only_egress_violation(role: ModelRole) -> None:
    with pytest.raises(EgressPolicyViolation):
        resolve_inference_request(role=role, data_class=DataClass.LOCAL_ONLY)


# --- Sensitive data on free-only reranker --------------------------------


def test_sensitive_rerank_unavailable() -> None:
    with pytest.raises(SensitiveRouteUnavailable):
        resolve_inference_request(
            role=ModelRole.RERANK, data_class=DataClass.CLOUD_APPROVED_SENSITIVE
        )


# --- Resolution matrix ---------------------------------------------------


def test_public_rerank_free_no_fallback() -> None:
    r = resolve_inference_request(role=ModelRole.RERANK, data_class=DataClass.PUBLIC)
    assert r.model == RER
    assert r.provider == "nvidia"
    assert r.params["provider"]["only"] == ["nvidia"]
    assert r.params["allow_fallbacks"] is False
    assert r.params["provider"]["data_collection"] == "none"


def test_public_generation_free_to_paid() -> None:
    r = resolve_inference_request(role=ModelRole.GENERATION, data_class=DataClass.PUBLIC)
    assert r.model == f"{GEN}:free"
    assert r.provider == "google-ai-studio"
    assert r.params["provider"]["only"] == ["google-ai-studio", "coreweave"]
    assert r.params["allow_fallbacks"] is True


def test_public_embedding_paid_only() -> None:
    r = resolve_inference_request(role=ModelRole.EMBEDDING, data_class=DataClass.PUBLIC)
    assert r.model == EMB
    assert r.provider == "google-ai-studio"
    assert r.params["provider"]["only"] == ["google-ai-studio", "google-vertex"]
    assert r.params["allow_fallbacks"] is False


def test_sensitive_generation_confidential() -> None:
    r = resolve_inference_request(
        role=ModelRole.GENERATION, data_class=DataClass.CLOUD_APPROVED_SENSITIVE
    )
    assert r.params["provider"]["only"] == ["google-ai-studio", "coreweave"]
    assert r.params["allow_fallbacks"] is True


def test_sensitive_embedding_paid() -> None:
    r = resolve_inference_request(
        role=ModelRole.EMBEDDING, model=EMB, data_class=DataClass.CLOUD_APPROVED_SENSITIVE
    )
    assert r.params["provider"]["only"] == ["google-ai-studio", "google-vertex"]
    assert r.params["allow_fallbacks"] is False


def test_headers_stamped() -> None:
    r = resolve_inference_request(role=ModelRole.GENERATION, data_class=DataClass.PUBLIC)
    assert r.headers["content-type"] == "application/json"
    assert r.headers["http-referer"] == "https://knowledge-os.local"
    assert r.headers["x-title"] == "knowledge-os"


# --- Unknown / unadmitted -> policy error --------------------------------


def test_unknown_model_policy_error() -> None:
    with pytest.raises(PolicyError):
        resolve_inference_request(role=ModelRole.EMBEDDING, model="bogus/model")


def test_unadmitted_role_policy_error() -> None:
    with pytest.raises(PolicyError):
        resolve_inference_request(role=ModelRole.VISION, data_class=DataClass.PUBLIC)


@pytest.mark.parametrize(
    "role",
    [ModelRole.GRAPH, ModelRole.AGENT, ModelRole.WORKFLOW],
)
def test_generative_roles_resolve_gemma_free_to_paid(role: ModelRole) -> None:
    r = resolve_inference_request(role=role, data_class=DataClass.PUBLIC)
    assert r.model == f"{GEN}:free"
    assert r.provider == "google-ai-studio"
    assert r.params["provider"]["only"] == ["google-ai-studio", "coreweave"]
    assert r.params["allow_fallbacks"] is True

    # Matching with :free slug explicitly
    r2 = resolve_inference_request(role=role, model=f"{GEN}:free", data_class=DataClass.PUBLIC)
    assert r2.model == f"{GEN}:free"

    # Matching with base slug explicitly
    r3 = resolve_inference_request(role=role, model=GEN, data_class=DataClass.PUBLIC)
    assert r3.model == f"{GEN}:free"

