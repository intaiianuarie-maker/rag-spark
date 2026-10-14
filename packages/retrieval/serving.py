"""T-R05 — the serving-name contract: a stable alias, never a delete-to-promote.

The outage shape this module exists to prevent
---------------------------------------------
``promote_replacement`` used to *delete the serving collection* and then re-issue
the serving name as an alias to the replacement. If the alias call failed, or the
process died in between, the serving name no longer resolved to anything: the
index was gone and nothing was promoting. Read-path availability must never
depend on a two-step destructive dance.

The contract, instead:

* the SERVING NAME is a stable **alias** that is adopted once, additively, while
  it points at the retained old collection (:func:`serving_alias_name` /
  ``QdrantVectorStore.ensure_serving_alias``) — the serving name is never
  deleted, so it keeps resolving through every step;
* promotion is **one atomic alias repoint** — a single
  ``update_collection_aliases`` request carrying both the delete-alias and the
  create-alias operation, so the alias either points at the old target or the new
  one, never nowhere;
* the superseded physical collection is **retained** (it is the rollback target)
  until an operator drops it after the rollback expiry;
* every failure path leaves a readable current-or-previous index and prints the
  recovery command.

Ambient-name drift (goal 2)
---------------------------
``core.config`` still defaults ``VECTOR_COLLECTION`` to ``knowledge``, and
neither the API nor the worker service sets it in compose — so both silently
serve whatever that default says. This module cannot edit those files (they are
outside T-R05's allowed paths), but it owns the *guard* that closes the drift at
the only place that matters: :func:`assert_explicit_collection` refuses a
promotion whose target came from the ambient default, naming both services and
the exact lines to add. The coordinator applies the one-line compose change; the
reindex promotion path enforces it.

No live-collection writes live here — this module is pure naming plus the guard.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: Suffix that turns a physical collection name into the stable serving alias.
SERVING_ALIAS_SUFFIX = "__serving"

#: The ambient default in ``core.config.Settings.vector_collection``. Tracked
#: here so the guard can name it; this module does not change that default.
AMBIENT_DEFAULT_COLLECTION = "knowledge"

#: The services whose configuration must carry VECTOR_COLLECTION explicitly.
SERVICES_REQUIRING_EXPLICIT_COLLECTION: tuple[str, ...] = ("api", "worker")

#: The exact compose lines the operator adds (idempotent, fail-closed).
COMPOSE_REMEDIATION: tuple[str, ...] = (
    "api:    VECTOR_COLLECTION: ${VECTOR_COLLECTION:?set VECTOR_COLLECTION to the serving alias}",
    "worker: VECTOR_COLLECTION: ${VECTOR_COLLECTION:?set VECTOR_COLLECTION to the serving alias}",
)


class ServingNameError(RuntimeError):
    """Raised when the serving name is unsafe to promote against."""


class ServingNameDrift(ServingNameError):
    """Raised when a service would serve an ambient (unset) collection name."""


@dataclass(frozen=True)
class ServingAliasState:
    """Where the serving alias points right now (read-only observation)."""

    alias: str
    #: The physical collection the alias serves, or ``None`` when the alias does
    #: not exist yet.
    target: str | None
    #: True when ``alias`` itself is a physical collection (the pre-T-R05 shape).
    serving_name_is_physical: bool

    @property
    def is_adopted(self) -> bool:
        return self.target is not None


def serving_alias_name(collection: str) -> str:
    """The stable serving alias for a physical collection name.

    Idempotent: naming an already-aliased name is a no-op, so callers can pass
    either the physical name or the alias.
    """
    if not collection:
        raise ServingNameError("collection name required")
    if collection.endswith(SERVING_ALIAS_SUFFIX):
        return collection
    return f"{collection}{SERVING_ALIAS_SUFFIX}"


def physical_name(name: str) -> str:
    """The physical base a serving name refers to (alias names map back)."""
    if name.endswith(SERVING_ALIAS_SUFFIX):
        return name[: -len(SERVING_ALIAS_SUFFIX)]
    return name


def assert_explicit_collection(
    env: Mapping[str, str],
    *,
    services: tuple[str, ...] = SERVICES_REQUIRING_EXPLICIT_COLLECTION,
) -> str:
    """The explicit ``VECTOR_COLLECTION``, or refuse.

    Refuses when the variable is absent, blank, or equal to the ambient default
    — the three ways a service ends up serving ``knowledge`` without anyone
    choosing it. The message names every service that must set it and the exact
    compose lines to add, so the fix is mechanical rather than a debate.
    """
    value = (env.get("VECTOR_COLLECTION") or "").strip()
    if not value:
        raise ServingNameDrift(
            "VECTOR_COLLECTION is not set, so the api and worker would fall back to the "
            f"ambient default {AMBIENT_DEFAULT_COLLECTION!r}. Set it explicitly in BOTH "
            f"services before promoting: " + "; ".join(COMPOSE_REMEDIATION)
        )
    if value == AMBIENT_DEFAULT_COLLECTION:
        raise ServingNameDrift(
            f"VECTOR_COLLECTION is the ambient default {AMBIENT_DEFAULT_COLLECTION!r}, which "
            "is the name promotion must never convert. Point both services at the stable "
            "serving alias instead: " + "; ".join(COMPOSE_REMEDIATION)
        )
    return value


def adopt_command(alias: str, physical: str) -> str:
    """The operator command that adopts a serving alias (additive, no writes)."""
    return (
        f"python -c \"import asyncio; from retrieval.adapters.qdrant_store import "
        f"QdrantVectorStore; asyncio.run(QdrantVectorStore().ensure_serving_alias("
        f"physical={physical!r}))\"   # creates alias {alias!r} -> {physical!r}"
    )


def rollback_command(serving: str, previous: str) -> str:
    """The operator command that repoints the serving alias back."""
    return (
        f"python -c \"import asyncio; from retrieval.adapters.qdrant_store import "
        f"QdrantVectorStore; asyncio.run(QdrantVectorStore().rollback_promotion("
        f"{serving!r}, to={previous!r}))\"   # atomic repoint back to {previous!r}"
    )


__all__ = [
    "AMBIENT_DEFAULT_COLLECTION",
    "COMPOSE_REMEDIATION",
    "SERVICES_REQUIRING_EXPLICIT_COLLECTION",
    "SERVING_ALIAS_SUFFIX",
    "ServingAliasState",
    "ServingNameDrift",
    "ServingNameError",
    "adopt_command",
    "assert_explicit_collection",
    "physical_name",
    "rollback_command",
    "serving_alias_name",
]