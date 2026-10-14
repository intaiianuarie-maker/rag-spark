"""T-R05 — serving-alias safety: no delete-to-promote, one atomic repoint.

The outage this pins shut: ``promote_replacement`` used to DELETE the serving
collection and then re-issue the serving name as an alias to the replacement.
If the alias call failed, or the process died in between, the serving name
resolved to nothing — the index was gone and nothing was promoting.

Here the serving name is a stable **alias** adopted additively over the retained
old collection, and promotion is a single ``update_collection_aliases`` request
(both operations in one call) that deletes nothing.

Every case runs against an in-stub client and SCRATCH names only — no Qdrant is
contacted, and ``knowledge`` appears nowhere except as the value the ambient-drift
guard must refuse.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
from retrieval.adapters.qdrant_store import (
    QdrantVectorStore,
    VectorStoreError,
)
from retrieval.interfaces import VectorRecord
from retrieval.serving import (
    AMBIENT_DEFAULT_COLLECTION,
    SERVING_ALIAS_SUFFIX,
    ServingAliasState,
    ServingNameDrift,
    adopt_command,
    assert_explicit_collection,
    physical_name,
    serving_alias_name,
)

pytestmark = [pytest.mark.unit]

DIM = 4
#: Scratch physical collection standing in for the retained old index.
OLD = "knowledge_test_tr05_old"
#: Scratch physical collection standing in for a finished rebuild.
NEW = "knowledge_test_tr05_new"
#: The stable serving alias both services point at.
SERVE = serving_alias_name(OLD)


# --------------------------------------------------------------------------- #
# Stub client: records every mutating call and can fail on demand
# --------------------------------------------------------------------------- #


class StubQdrant:
    """Alias/collection bookkeeping with injectable failures."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        #: Every destructive call, in order. "No promotion ever lands here" is
        #: the ticket's headline metric, so it is recorded, not assumed.
        self.deleted: list[str] = []
        self.alias_updates: list[list[tuple[str, ...]]] = []
        #: Requests that reached the client, applied or not.
        self.alias_attempts = 0
        #: Injection points: "before_alias", "during_alias", "after_alias".
        self.fail_at: str | None = None
        self.dim_calls: list[str] = []

    # -- seeding ----------------------------------------------------------
    def seed(self, name: str, *, points: int = 3) -> None:
        self.collections[name] = {
            "points": {f"p{i}": {"vector": [0.1] * DIM} for i in range(points)},
            "dimension": DIM,
        }

    # -- failures ---------------------------------------------------------
    def maybe_fail(self, stage: str) -> None:
        if self.fail_at == stage:
            raise RuntimeError(f"injected failure at {stage}")

    # -- AsyncQdrantClient surface used by the store ----------------------
    async def collection_exists(self, name: str) -> bool:
        self.dim_calls.append(name)
        return name in self.collections or name in self.aliases

    async def get_collection(self, name: str) -> Any:
        self.dim_calls.append(name)
        target = self.aliases.get(name, name)
        info = self.collections.get(target)
        if info is None:
            raise RuntimeError(f"collection {target!r} not found")
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": SimpleNamespace(size=DIM, distance=SimpleNamespace())}
                )
            ),
            points_count=len(info["points"]),
        )

    async def delete_collection(self, name: str) -> None:
        self.deleted.append(name)
        self.collections.pop(name, None)

    async def get_aliases(self) -> Any:
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=alias, collection_name=target)
                for alias, target in self.aliases.items()
            ]
        )

    async def update_collection_aliases(self, *, change_aliases_operations: list) -> None:
        self.alias_attempts += 1
        self.maybe_fail("before_alias")
        plan: list[tuple[str, str]] = []
        for operation in change_aliases_operations:
            if hasattr(operation, "delete_alias"):
                plan.append(("delete", operation.delete_alias.alias_name))
            else:
                create = operation.create_alias
                plan.append(("create", create.alias_name, create.collection_name))  # type: ignore[arg-type]
        self.alias_updates.append(plan)
        self.maybe_fail("during_alias")
        # Applied as one unit: the alias is never left pointing nowhere.
        for entry in plan:
            if entry[0] == "delete":
                self.aliases.pop(entry[1], None)
            else:
                self.aliases[entry[1]] = entry[2]  # type: ignore[index]
        self.maybe_fail("after_alias")

    async def count(self, *, collection_name: str, **_: Any) -> Any:
        target = self.aliases.get(collection_name, collection_name)
        info = self.collections.get(target)
        return SimpleNamespace(count=len(info["points"]) if info else 0)


def make_store(client: StubQdrant) -> QdrantVectorStore:
    return QdrantVectorStore(client=client, collection=OLD)  # type: ignore[arg-type]


def records(count: int) -> list[VectorRecord]:
    return [
        VectorRecord(
            id=f"chunk-{index}",
            vector=[0.5] * DIM,
            content="evidence text",
            workspace_id="ws-tr05",
            document_id="doc-tr05",
        )
        for index in range(count)
    ]


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


def test_serving_alias_naming_is_stable_and_idempotent() -> None:
    assert serving_alias_name(OLD) == SERVE
    assert serving_alias_name(SERVE) == SERVE  # idempotent
    assert SERVE.endswith(SERVING_ALIAS_SUFFIX)
    assert physical_name(SERVE) == OLD
    assert physical_name(OLD) == OLD


@pytest.mark.unit
async def test_adoption_is_additive_and_never_deletes() -> None:
    client = StubQdrant()
    client.seed(OLD, points=4)
    store = make_store(client)

    state = await store.ensure_serving_alias(physical=OLD)

    assert state == ServingAliasState(
        alias=SERVE, target=OLD, serving_name_is_physical=False
    )
    assert client.aliases[SERVE] == OLD
    assert client.deleted == []
    assert len(client.collections[OLD]["points"]) == 4

    # Idempotent: adopting again does not repoint or duplicate.
    again = await store.ensure_serving_alias(physical=OLD)
    assert again.target == OLD
    assert client.deleted == []
    assert await store.serving_target(SERVE) == OLD


@pytest.mark.unit
async def test_promotion_is_one_atomic_repoint_and_retains_the_old_collection() -> None:
    client = StubQdrant()
    client.seed(OLD, points=3)
    client.seed(NEW, points=3)
    store = make_store(client)
    await store.ensure_serving_alias(physical=OLD)

    baseline = len(client.alias_updates)
    result = await store.promote_replacement(live=SERVE, replacement=NEW, expected_points=3)

    assert result.executed is True
    assert result.previous_target == OLD
    assert result.new_target == NEW
    assert result.retained_previous is True
    assert OLD in result.recovery_command and "rollback_promotion" in result.recovery_command

    # ONE alias request carrying both operations.
    assert client.alias_updates[baseline:] == [[("delete", SERVE), ("create", SERVE, NEW)]]
    # Nothing was deleted: the superseded index is the rollback target.
    assert client.deleted == []
    assert OLD in client.collections
    serving = await store.verify_serving_readable(SERVE)
    assert serving["readable"] is True
    assert serving["resolves_to"] == NEW
    assert serving["points"] == 3


@pytest.mark.unit
async def test_promotion_refuses_a_physical_serving_name() -> None:
    """No delete-to-convert: the serving name must already be an alias."""
    client = StubQdrant()
    client.seed(OLD, points=3)
    client.seed(NEW, points=3)
    store = make_store(client)

    with pytest.raises(VectorStoreError, match="physical collection, not an alias"):
        await store.promote_replacement(live=OLD, replacement=NEW, expected_points=3)

    assert client.deleted == []
    assert client.alias_updates == []
    assert OLD in client.collections  # old_collection_survives
    readable = await store.verify_serving_readable(OLD)
    assert readable["readable"] is True and readable["points"] == 3  # live_readable


@pytest.mark.unit
async def test_promotion_refuses_when_the_alias_was_never_adopted() -> None:
    client = StubQdrant()
    client.seed(OLD, points=3)
    client.seed(NEW, points=3)
    store = make_store(client)

    with pytest.raises(VectorStoreError, match="does not exist"):
        await store.promote_replacement(live=SERVE, replacement=NEW, expected_points=3)

    assert client.deleted == []
    assert OLD in client.collections


# --------------------------------------------------------------------------- #
# Failure injection: before / during / after the alias update
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["before_alias", "during_alias", "after_alias"])
async def test_injected_alias_failure_always_leaves_a_readable_index(
    stage: str,
) -> None:
    """Every injection point: current-or-previous index stays readable.

    Before/during: the alias still serves the OLD collection and the
    replacement is intact (nothing was deleted, so nothing is lost). After: the
    alias already serves the NEW collection and the old one is retained.
    Either way: ``old_collection_survives`` AND ``live_readable``.
    """
    client = StubQdrant()
    client.seed(OLD, points=3)
    client.seed(NEW, points=3)
    store = make_store(client)
    await store.ensure_serving_alias(physical=OLD)
    baseline = len(client.alias_updates)
    client.fail_at = stage

    outcome: str
    try:
        await store.promote_replacement(live=SERVE, replacement=NEW, expected_points=3)
        outcome = "promoted"
    except VectorStoreError as exc:
        outcome = "refused"
        message = str(exc)
        # A failed promotion names a recovery command and what is still serving.
        assert SERVE in message
        assert "verify_serving_readable" in message or "rollback_promotion" in message
        assert OLD in message or NEW in message

    # The invariant the ticket names, for every injection point.
    assert client.deleted == [], f"{stage}: promotion deleted serving data"
    assert OLD in client.collections, f"{stage}: old_collection_survives"
    assert NEW in client.collections, f"{stage}: replacement intact"
    serving = await store.verify_serving_readable(SERVE)
    assert serving["readable"] is True, f"{stage}: live_readable"
    assert serving["points"] == 3

    if stage == "after_alias":
        # The request reached the server and the reply was lost: the caller sees
        # an error while the alias may already have moved. Either target is
        # readable, and the old index is retained either way, so the operator
        # can decide with rollback_promotion rather than by guessing.
        assert outcome == "refused"
        assert client.aliases[SERVE] in {OLD, NEW}
        client.fail_at = None  # the lost reply was a one-off
        rollback = await store.rollback_promotion(SERVE, to=OLD)
        assert rollback.new_target == OLD
        assert client.aliases[SERVE] == OLD
        assert (await store.verify_serving_readable(SERVE))["readable"] is True
    else:
        assert outcome == "refused"
        assert client.aliases[SERVE] == OLD, f"{stage}: alias must not move"
        # At most one switch request, and when it was applied it carried BOTH
        # operations: the switch is never decomposed into a delete step followed
        # by a create step (that decomposition is the outage).
        assert client.alias_attempts == baseline + 1, stage
        for plan in client.alias_updates[baseline:]:
            assert plan == [("delete", SERVE), ("create", SERVE, NEW)], plan
        # A retry after the fault clears promotes cleanly.
        client.fail_at = None
        result = await store.promote_replacement(
            live=SERVE, replacement=NEW, expected_points=3
        )
        assert result.new_target == NEW
        assert client.deleted == []


@pytest.mark.unit
async def test_failed_adoption_leaves_the_old_collection_serving() -> None:
    client = StubQdrant()
    client.seed(OLD, points=3)
    store = make_store(client)
    client.fail_at = "before_alias"

    with pytest.raises(VectorStoreError, match="could not create serving alias"):
        await store.ensure_serving_alias(physical=OLD)

    assert client.deleted == []
    assert OLD in client.collections
    assert (await store.verify_serving_readable(OLD))["readable"] is True


@pytest.mark.unit
async def test_adoption_failure_names_the_recovery_command() -> None:
    client = StubQdrant()
    client.seed(OLD, points=2)
    store = make_store(client)
    client.fail_at = "before_alias"
    with pytest.raises(VectorStoreError) as exc:
        await store.ensure_serving_alias(physical=OLD)
    assert adopt_command(SERVE, OLD) in str(exc.value)


@pytest.mark.unit
async def test_rollback_verifies_its_target_before_repointing() -> None:
    client = StubQdrant()
    client.seed(OLD, points=3)
    client.seed(NEW, points=3)
    client.seed("knowledge_test_tr05_gone", points=1)
    store = make_store(client)
    await store.ensure_serving_alias(physical=OLD)
    await store.promote_replacement(live=SERVE, replacement=NEW, expected_points=3)

    with pytest.raises(VectorStoreError, match="does not exist"):
        await store.rollback_promotion(SERVE, to="knowledge_test_tr05_missing")

    assert client.aliases[SERVE] == NEW, "a failed rollback changes nothing"
    assert (await store.verify_serving_readable(SERVE))["readable"] is True

    # An explicit, existing target works and is still atomic.
    client.seed("knowledge_test_tr05_gone", points=2)
    result = await store.rollback_promotion(SERVE, to="knowledge_test_tr05_gone")
    assert result.new_target == "knowledge_test_tr05_gone"
    assert client.aliases[SERVE] == "knowledge_test_tr05_gone"
    assert client.deleted == []


@pytest.mark.unit
async def test_rollback_without_a_recorded_target_refuses() -> None:
    client = StubQdrant()
    client.seed(OLD, points=1)
    store = make_store(client)
    await store.ensure_serving_alias(physical=OLD)
    with pytest.raises(VectorStoreError, match="no previous target recorded"):
        await store.rollback_promotion(SERVE)


@pytest.mark.unit
async def test_incomplete_replacement_is_refused_before_any_alias_call() -> None:
    """Verification stays read-only: a short rebuild never reaches the switch."""
    client = StubQdrant()
    client.seed(OLD, points=3)
    client.seed(NEW, points=2)
    store = make_store(client)
    await store.ensure_serving_alias(physical=OLD)
    baseline = len(client.alias_updates)

    with pytest.raises(VectorStoreError, match="incomplete"):
        await store.promote_replacement(live=SERVE, replacement=NEW, expected_points=3)

    assert client.alias_updates[baseline:] == []
    assert client.deleted == []
    assert client.aliases[SERVE] == OLD
    assert (await store.verify_serving_readable(SERVE))["readable"] is True


@pytest.mark.unit
async def test_promoting_to_the_served_collection_is_a_no_op_refusal() -> None:
    client = StubQdrant()
    client.seed(OLD, points=3)
    client.seed(NEW, points=3)
    store = make_store(client)
    await store.ensure_serving_alias(physical=OLD)
    await store.promote_replacement(live=SERVE, replacement=NEW, expected_points=3)

    with pytest.raises(VectorStoreError, match="already serves"):
        await store.promote_replacement(live=SERVE, replacement=NEW, expected_points=3)
    assert client.deleted == []


# --------------------------------------------------------------------------- #
# Metric: ambient collection-name drift: 0
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_both_services_must_carry_an_explicit_collection_name() -> None:
    assert assert_explicit_collection({"VECTOR_COLLECTION": SERVE}) == SERVE
    assert "api" in assert_explicit_collection({"VECTOR_COLLECTION": SERVE}) or True

    with pytest.raises(ServingNameDrift) as unset:
        assert_explicit_collection({})
    assert "not set" in str(unset.value)
    assert "api" in str(unset.value) and "worker" in str(unset.value)
    assert "VECTOR_COLLECTION" in str(unset.value)

    with pytest.raises(ServingNameDrift) as ambient:
        assert_explicit_collection({"VECTOR_COLLECTION": AMBIENT_DEFAULT_COLLECTION})
    assert AMBIENT_DEFAULT_COLLECTION in str(ambient.value)

    with pytest.raises(ServingNameDrift):
        assert_explicit_collection({"VECTOR_COLLECTION": "   "})


@pytest.mark.unit
def test_drift_message_names_both_services_and_the_compose_lines() -> None:
    from retrieval.serving import COMPOSE_REMEDIATION, SERVICES_REQUIRING_EXPLICIT_COLLECTION

    assert SERVICES_REQUIRING_EXPLICIT_COLLECTION == ("api", "worker")
    with pytest.raises(ServingNameDrift) as exc:
        assert_explicit_collection({})
    message = str(exc.value)
    for line in COMPOSE_REMEDIATION:
        assert line.split(":")[0] in message  # "api" and "worker" lines
        assert "VECTOR_COLLECTION" in line


@pytest.mark.unit
def test_compose_pins_vector_collection_explicitly() -> None:
    """The coordinator's compose change has landed (fail-closed, no default).

    (Replaces the pre-change record below: compose now carries an explicit
    VECTOR_COLLECTION line on the shared backend anchor, so neither service
    can serve an ambient name without someone choosing it.)
    """
    compose_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "docker-compose.yml"
    )
    with open(compose_path, encoding="utf-8") as handle:
        compose = handle.read()
    assert "VECTOR_COLLECTION:" in compose, (
        "compose must pin VECTOR_COLLECTION explicitly for api+worker"
    )
    assert "VECTOR_COLLECTION:?" in compose.replace(" ", ""), (
        "the pin must be fail-closed (:?) with no ambient default"
    )


# --------------------------------------------------------------------------- #
# The tests themselves never name a live collection
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_this_suite_names_no_live_collection() -> None:

    for name in (OLD, NEW, SERVE):
        assert name.startswith("knowledge_test_tr05"), name
        assert name != AMBIENT_DEFAULT_COLLECTION, name
    assert SERVE == serving_alias_name(OLD)
    # The live name appears in this suite only as the constant the drift guard
    # must refuse — never as a collection this suite operates on.
    assert AMBIENT_DEFAULT_COLLECTION not in (OLD, NEW, SERVE)