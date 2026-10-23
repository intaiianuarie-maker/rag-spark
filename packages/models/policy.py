"""Model & provider policy contracts (T-OR-01).

Project-owned, vendor-free domain contracts for model routing, provider
admission and data-handling policy. These immutable value types back the
OpenRouter transport (T-OR-02): no third-party SDK type crosses this boundary.

The companion :mod:`packages.models.catalog` is the single source of truth for
which models are *admitted* and how a route is resolved under the fail-closed
data-class rules:

* ``public``                  -> any admitted route (free or paid) is eligible.
* ``cloud_approved_sensitive``-> free endpoints that forbid confidential data
  are rejected; only endpoints explicitly approved for sensitive data run.
* ``local_only``              -> remote egress is always vetoed
  (:class:`EgressPolicyViolation`).

These types are deliberately plain dataclasses/enums (no vendor SDK) so they
can be shared by the retrieval, rerank, answering and transport layers without
leaking a provider surface into core business logic (see AGENTS.md §2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ModelRole(StrEnum):
    """Pipeline role a model is admitted to serve."""

    EMBEDDING = "embedding"
    RERANK = "rerank"
    GENERATION = "generation"
    SEMANTIC_VERIFICATION = "semantic_verification"
    VISION = "vision"
    GRAPH = "graph"
    AGENT = "agent"
    WORKFLOW = "workflow"


class Tier(StrEnum):
    """Billing/latency tier a route is permitted to use.

    ``free_first`` — a free endpoint is tried first; a pre-approved paid
    fallback of the same model family is used on pool exhaustion.
    ``paid_only``  — no free endpoint is ever admitted.
    """

    FREE_FIRST = "free_first"
    PAID_ONLY = "paid_only"


class DataClass(StrEnum):
    """Sensitivity class of the data bound for an inference route.

    Drives the fail-closed resolution rules in
    :mod:`packages.models.catalog`.
    """

    PUBLIC = "public"
    CLOUD_APPROVED_SENSITIVE = "cloud_approved_sensitive"
    LOCAL_ONLY = "local_only"


class Modality(StrEnum):
    """Input modalities an endpoint/model can accept."""

    TEXT = "text"
    IMAGE = "image"


class DataCollectionPolicy(StrEnum):
    """Vendor-side data-handling stance required on every admitted route.

    ``deny`` means the serving provider must not log or train on the payloads
    it receives (OpenRouter's ``provider.data_collection=none`` stance).
    """

    DENY = "deny"


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """Endpoint/model capability surface."""

    modalities: tuple[Modality, ...] = (Modality.TEXT,)
    structured_output: bool = False
    tools: bool = False


@dataclass(frozen=True)
class Lineage:
    """Model provenance evidence, checked by the non-China origin guard.

    ``base_model`` is the underlying foundation-model family; ``publisher`` is
    the organisation that produced it. Neither may originate from a
    restricted jurisdiction (see :mod:`packages.models.catalog`).
    """

    base_model: str
    publisher: str


@dataclass(frozen=True)
class ModelRoute:
    """One admitted model slug together with its routing policy.

    ``permitted_tiers`` declares which billing tiers this route may use; the
    ``lineage`` evidence is checked against the non-China guard at admission.
    """

    model: str
    role: ModelRole
    permitted_tiers: tuple[Tier, ...]
    lineage: Lineage
    capability: Capability = field(default_factory=Capability)


@dataclass(frozen=True)
class ProviderPolicy:
    """Per-provider admission policy attached to a route.

    ``allowed_providers`` backs OpenRouter ``provider.only``. It MUST be a
    non-empty allowlist — an empty list is **fail-closed**
    (:class:`EmptyProviderAllowlist`); unrestricted routing is never permitted.
    """

    allowed_providers: tuple[str, ...]
    zdr_required: bool = False
    data_collection: DataCollectionPolicy = DataCollectionPolicy.DENY
    regions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedInferenceRequest:
    """A concretely resolved inference call ready for transport.

    ``params`` carries the OpenRouter body parameters (notably
    ``provider.only`` and ``allow_fallbacks``); ``headers`` carries the authn
    envelope the transport stamps in. The transport serializes these fields
    verbatim and never re-derives the model or widens ``provider.only``.
    """

    model: str
    provider: str
    params: dict[str, Any]
    headers: dict[str, str]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PolicyError(RuntimeError):
    """Base for typed policy verdicts raised by the catalogue / transport."""


class AdmissionError(PolicyError):
    """Catalogue admission failure (a route was rejected at build time)."""


class EmptyProviderAllowlist(AdmissionError):
    """Fail-closed: a route admitted with an empty ``provider.only`` allowlist."""


class ChinaOriginRejected(AdmissionError):
    """Fail-closed: base-model lineage or serving provider is restricted."""


class EgressPolicyViolation(PolicyError):
    """``local_only`` data must not be serialized to a remote endpoint."""


class SensitiveRouteUnavailable(PolicyError):
    """No confidential-approved endpoint is available for sensitive data.

    Raised when a free endpoint forbids confidential data (e.g. the NVIDIA free
    reranker) and no approved paid fallback of the same family exists. Callers
    catch this to degrade to a controlled no-rerank / unavailable verdict.
    """
