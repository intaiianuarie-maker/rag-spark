"""T-V4-017 — versioned draft/revision store (in-memory) tests.

Pinned properties (card):
1. History is append-only: **no** update / delete / overwrite path exists on
   the store surface, and an early revision reads back byte-identical after
   later revisions are appended.
2. Revision identity has **0 collisions**: identical content hashes
   identically (across processes, i.e. under a different ``PYTHONHASHSEED``)
   and any byte change changes the hash.
3. Revisions are monotonic per workflow and never reused.
4. Store discipline: bounded payloads, secrets refused at the boundary,
   redacted bounded summaries, deep-copy immutability.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest
from workflow_engine import (
    NodeSpec,
    NodeType,
    PortSpec,
    WorkflowDefinition,
    WorkflowEdgeSpec,
)
from workflow_engine.draft_store import (
    DraftConflictError,
    DraftIdentityError,
    DraftNotFoundError,
    DraftRevision,
    DraftStore,
    DraftWorkspaceError,
    InMemoryDraftStore,
    assert_append_only,
    verify_revision,
)
from workflow_engine.store_discipline import (
    MAX_PAYLOAD_BYTES,
    StoreBoundsError,
    StoreSecretError,
    assert_no_secret_config,
    enforce_bounds,
    redact_text,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _defn(
    *,
    wid: str = "wf-draft",
    workspace: str = "ws-1",
    name: str = "Draft",
    node_name: str = "fmt",
    description: str = "",
    extra_nodes: int = 0,
) -> WorkflowDefinition:
    nodes = [
        NodeSpec(
            id="start",
            type=NodeType.TRIGGER,
            name="Start",
            output_ports=[PortSpec(name="output", type="dict")],
        ),
        NodeSpec(
            id="fmt",
            type=NodeType.TRANSFORM,
            name=node_name,
            config={"template": "Hi {name}!"},
            input_ports=[PortSpec(name="input", type="any")],
            output_ports=[PortSpec(name="output", type="string")],
        ),
    ]
    for i in range(extra_nodes):
        nodes.append(
            NodeSpec(
                id=f"extra-{i}",
                type=NodeType.OUTPUT,
                name=f"Extra {i}",
                input_ports=[PortSpec(name="input", type="any")],
                output_ports=[PortSpec(name="output", type="any")],
            )
        )
    return WorkflowDefinition(
        id=wid, workspace_id=workspace, name=name, description=description,
        nodes=nodes,
        edges=[
            WorkflowEdgeSpec(
                id="e1", source_node_id="start", source_port="output",
                target_node_id="fmt", target_port="input",
            )
        ],
    )


def _canonical(spec: WorkflowDefinition) -> str:
    return json.dumps(spec.model_dump(mode="json"), sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# 1. Append-only — no update / delete / overwrite path exists
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_store_surface_has_no_history_mutating_method() -> None:
    """The append-only guarantee is structural: such methods do not exist."""
    store = InMemoryDraftStore()
    assert_append_only(store)  # raises if any mutate/delete path is exposed

    for name in ("update", "delete", "remove", "overwrite", "replace",
                 "clear", "truncate", "rewrite", "purge"):
        assert not hasattr(store, name), f"{name} must not exist on the store"


@pytest.mark.unit
def test_abc_has_no_mutating_write_beyond_create() -> None:
    """The ABC contract itself offers only create/read/list verbs."""
    verbs = sorted(
        n for n in dir(DraftStore)
        if not n.startswith("_") and callable(getattr(DraftStore, n, None))
    )
    assert verbs == [
        "count", "get", "history", "latest", "revise", "save_draft", "workflow_ids",
    ]


@pytest.mark.unit
async def test_early_revision_reads_back_byte_identical_after_later_writes() -> None:
    """Appending revisions must not disturb what is already stored."""
    store = InMemoryDraftStore()
    first = await store.save_draft(_defn(name="v1"))
    snapshot = _canonical(first.spec)

    await store.revise("wf-draft", _defn(name="v2", node_name="fmt2"))
    await store.revise("wf-draft", _defn(name="v3", extra_nodes=2))
    await store.revise("wf-draft", _defn(name="v4"))

    reread = await store.get("wf-draft", 1)
    assert _canonical(reread.spec) == snapshot
    assert reread.content_hash == first.content_hash
    assert reread.number == 1
    assert verify_revision(reread.revision)


@pytest.mark.unit
async def test_stored_revision_is_not_mutable_through_a_returned_copy() -> None:
    """Mutating a returned revision cannot rewrite history."""
    store = InMemoryDraftStore()
    await store.save_draft(_defn())
    got = await store.get("wf-draft", 1)

    got.spec.nodes[1].name = "TAMPERED"
    assert verify_revision(got.revision) is False  # the copy no longer matches
    assert (await store.get("wf-draft", 1)).spec.nodes[1].name == "fmt"
    assert verify_revision((await store.get("wf-draft", 1)).revision)


@pytest.mark.unit
async def test_caller_mutation_after_save_does_not_change_the_store() -> None:
    """The store keeps its own copy of what it was handed."""
    store = InMemoryDraftStore()
    defn = _defn()
    await store.save_draft(defn)
    defn.nodes[1].name = "mutated after save"

    assert (await store.get("wf-draft", 1)).spec.nodes[1].name == "fmt"


# --------------------------------------------------------------------------- #
# 2. Revision identity — 0 collisions
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_identical_content_hashes_identically() -> None:
    """Same bytes -> same revision identity (and no history growth)."""
    store = InMemoryDraftStore()
    a = await store.save_draft(_defn())
    again = await store.revise("wf-draft", _defn())

    assert again.content_hash == a.content_hash
    assert again.number == a.number == 1
    assert await store.count("wf-draft") == 1


@pytest.mark.unit
async def test_any_byte_change_changes_the_hash() -> None:
    """Identity is content-addressed: every byte matters."""
    store = InMemoryDraftStore()
    base = await store.save_draft(_defn())

    variants = {
        "description byte": _defn(description="a"),
        "second byte": _defn(description="ab"),
        "node name": _defn(node_name="fmt2"),
        "node added": _defn(extra_nodes=1),
        "workflow name": _defn(name="Draft 2"),
        "trailing space": _defn(description="a "),
    }
    for label, variant in variants.items():
        got = await store.revise("wf-draft", variant)
        assert got.content_hash != base.content_hash, (
            f"{label} must change revision identity"
        )
        assert got.number != base.number

    assert await store.count("wf-draft") == 1 + len(variants)


@pytest.mark.unit
async def test_no_hash_collisions_across_distinct_definitions() -> None:
    """Card metric: revision identity collisions = 0.

    Every distinct definition gets a distinct identity, and every identity
    maps back to exactly the content that produced it.
    """
    store = InMemoryDraftStore()
    definitions = [
        _defn(wid="wf-0", name="A"),
        _defn(wid="wf-1", name="B"),
        _defn(wid="wf-2", node_name="x"),
        _defn(wid="wf-3", extra_nodes=1),
        _defn(wid="wf-4", description="d"),
        _defn(wid="wf-5", workspace="ws-2"),
    ]
    hashes: dict[str, str] = {}
    for defn in definitions:
        record = await store.save_draft(defn)
        assert record.content_hash not in hashes, (
            f"identity collision between {hashes.get(record.content_hash)} "
            f"and {defn.id}"
        )
        hashes[record.content_hash] = defn.id
        # Identity maps back to exactly this content.
        assert _canonical((await store.get(defn.id, 1)).spec) == _canonical(defn)


@pytest.mark.unit
def test_hash_is_process_independent() -> None:
    """Identical bytes hash identically under a different PYTHONHASHSEED.

    ``workspace_capabilities`` is a set, whose iteration order follows the
    process hash seed; the canonicalizer (T-V4-008) must sort it, or the same
    draft would get a different identity per process.
    """
    packages_dir = Path(__file__).resolve().parents[2] / "packages"
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(packages_dir)!r});"
        "from workflow_engine import WorkflowDefinition, WorkflowRevision;"
        "d = WorkflowDefinition(id='wf-seed', workspace_id='ws', name='n',"
        " workspace_capabilities={'llm','retrieval','tool','http','embed'});"
        "print(WorkflowRevision.create(d).content_hash)"
    )
    hashes = set()
    for seed in ("0", "1", "12345", "99999"):
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        assert out.stdout.strip(), f"seed {seed} produced no hash: {out.stderr}"
        hashes.add(out.stdout.strip())
    assert len(hashes) == 1, f"hash must be seed-independent, got {hashes}"


@pytest.mark.unit
def test_hash_changes_under_capability_set_reordering_only_if_bytes_change() -> None:
    """A capability-set reordering with the same members is NOT a change."""
    first = WorkflowDefinition(
        id="wf-h", workspace_id="ws", name="n",
        workspace_capabilities={"llm", "retrieval"},
    )
    second = WorkflowDefinition(
        id="wf-h", workspace_id="ws", name="n",
        workspace_capabilities={"retrieval", "llm"},
    )
    from workflow_engine import WorkflowRevision

    assert WorkflowRevision.create(first).content_hash == (
        WorkflowRevision.create(second).content_hash
    )


# --------------------------------------------------------------------------- #
# 3. Monotonic revisions
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_revisions_are_monotonic_and_never_reused() -> None:
    store = InMemoryDraftStore()
    await store.save_draft(_defn())
    seq = [
        (await store.revise("wf-draft", _defn(name=f"v{i}"))).number
        for i in range(2, 7)
    ]
    assert seq == [2, 3, 4, 5, 6]

    history = await store.history("wf-draft")
    assert [h.number for h in history] == [1, 2, 3, 4, 5, 6]
    for earlier, later in pairwise(history):
        assert later.parent_revision == earlier.number
        assert later.parent_hash == earlier.content_hash


@pytest.mark.unit
async def test_revert_to_old_content_is_a_new_revision_not_a_rewrite() -> None:
    """History is append-only even when content already existed."""
    store = InMemoryDraftStore()
    await store.save_draft(_defn(name="v1"))
    await store.revise("wf-draft", _defn(name="v2"))

    # Content that already exists as revision 1: a *new* revision, not a
    # rewrite of revision 1, and the earlier revision is undisturbed.
    reverted = await store.revise("wf-draft", _defn(name="v1"))
    assert reverted.number == 3
    assert reverted.content_hash == (await store.get("wf-draft", 1)).content_hash
    assert await store.count("wf-draft") == 3

    assert (await store.get("wf-draft", 1)).spec.name == "v1"
    assert (await store.get("wf-draft", 2)).spec.name == "v2"


@pytest.mark.unit
async def test_revise_on_unknown_workflow_initialises_revision_one() -> None:
    store = InMemoryDraftStore()
    record = await store.revise("wf-fresh", _defn(wid="wf-fresh"))
    assert record.number == 1
    assert record.parent_revision is None
    assert record.parent_hash is None


@pytest.mark.unit
async def test_save_draft_on_existing_workflow_refuses() -> None:
    store = InMemoryDraftStore()
    await store.save_draft(_defn())
    with pytest.raises(DraftConflictError):
        await store.save_draft(_defn(name="v2"))


# --------------------------------------------------------------------------- #
# Reads / errors
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_get_unknown_workflow_or_revision_refuses() -> None:
    store = InMemoryDraftStore()
    await store.save_draft(_defn())
    with pytest.raises(DraftNotFoundError):
        await store.get("wf-other", 1)
    with pytest.raises(DraftNotFoundError):
        await store.get("wf-draft", 99)
    with pytest.raises(DraftNotFoundError):
        await store.get("wf-draft", 0)


@pytest.mark.unit
async def test_latest_and_workflow_ids() -> None:
    store = InMemoryDraftStore()
    assert await store.latest("nope") is None
    assert await store.workflow_ids() == []

    await store.save_draft(_defn(wid="wf-b"))
    await store.save_draft(_defn(wid="wf-a", workspace="ws-1"))
    latest = await store.revise("wf-a", _defn(wid="wf-a", name="v2"))

    assert (await store.latest("wf-a")).number == latest.number
    assert await store.workflow_ids() == ["wf-a", "wf-b"]


@pytest.mark.unit
async def test_history_returns_copies_ascending() -> None:
    store = InMemoryDraftStore()
    await store.save_draft(_defn())
    await store.revise("wf-draft", _defn(name="v2"))
    history = await store.history("wf-draft")

    assert all(isinstance(h, DraftRevision) for h in history)
    assert [h.number for h in history] == [1, 2]
    history[0].spec.nodes[0].name = "TAMPER"
    assert (await store.history("wf-draft"))[0].spec.nodes[0].name == "Start"


# --------------------------------------------------------------------------- #
# Identity / workspace guards
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_revise_with_mismatched_definition_id_refuses() -> None:
    store = InMemoryDraftStore()
    await store.save_draft(_defn())
    with pytest.raises(DraftIdentityError):
        await store.revise("wf-draft", _defn(wid="wf-something-else"))


@pytest.mark.unit
async def test_cross_workspace_revision_refuses() -> None:
    """A revision may not move a workflow into another workspace."""
    store = InMemoryDraftStore()
    await store.save_draft(_defn())
    with pytest.raises(DraftWorkspaceError):
        await store.revise("wf-draft", _defn(workspace="ws-2"))
    # History is untouched by the refusal.
    assert await store.count("wf-draft") == 1


@pytest.mark.unit
async def test_refused_write_leaves_history_untouched() -> None:
    store = InMemoryDraftStore()
    await store.save_draft(_defn())
    before = _canonical((await store.get("wf-draft", 1)).spec)

    bad = _defn()
    bad.nodes[1].config = {"authorization": "Bearer abcdefghijklmnopqrst"}
    with pytest.raises(StoreSecretError):
        await store.revise("wf-draft", bad)

    assert await store.count("wf-draft") == 1
    assert _canonical((await store.get("wf-draft", 1)).spec) == before


# --------------------------------------------------------------------------- #
# Store discipline: bounded, secrets, redacted summary
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_summary_is_bounded_and_redacted() -> None:
    store = InMemoryDraftStore()
    await store.save_draft(_defn())
    record = await store.get("wf-draft", 1)
    assert 0 < len(record.summary) <= 1_024
    assert "wf-draft" in record.summary

    # A secret-shaped value in a *description* (not config) is redacted from
    # the summary projection rather than refused.
    sneaky = _defn(name="v2", description="api_key=" + "s" * 30)
    await store.revise("wf-draft", sneaky)
    summary = (await store.latest("wf-draft")).summary
    assert "api_key=" + "s" * 30 not in summary


@pytest.mark.unit
async def test_oversized_definition_is_refused_not_truncated() -> None:
    """Oversized payloads are refused; in-range payloads pass through as-is."""
    # In range: the payload is returned unchanged (no silent truncation).
    small = _defn()
    assert enforce_bounds(_canonical(small), limit=MAX_PAYLOAD_BYTES) == _canonical(small)

    # Oversized: many fields, each individually under MAX_TEXT_CHARS, so only
    # the aggregate can breach the bound.
    big = _defn(extra_nodes=400)
    for node in big.nodes[2:]:
        node.config = {"blob": "x" * 4096}
    with pytest.raises(StoreBoundsError):
        enforce_bounds(_canonical(big), limit=MAX_PAYLOAD_BYTES)

    store = InMemoryDraftStore()
    with pytest.raises(StoreBoundsError):
        await store.save_draft(big)
    assert await store.count(big.id) == 0
    assert (await store.latest(big.id)) is None


@pytest.mark.unit
async def test_secret_in_node_config_is_refused() -> None:
    store = InMemoryDraftStore()
    sneaky = _defn()
    sneaky.nodes[1].config["headers"] = {"authorization": "Bearer " + "z" * 24}
    with pytest.raises(StoreSecretError):
        await store.save_draft(sneaky)


@pytest.mark.unit
def test_secret_guard_targets_config_only() -> None:
    """A node *named* auth is not a secret channel; config keys are."""
    named = _defn()
    named.nodes[1].name = "auth_check"
    assert_no_secret_config(named)  # does not raise

    configured = _defn()
    configured.nodes[1].config = {"password": "hunter2"}
    with pytest.raises(StoreSecretError):
        assert_no_secret_config(configured)


@pytest.mark.unit
def test_redact_text_clears_secret_material() -> None:
    assert "Bearer " + "q" * 20 not in redact_text(
        "Authorization: Bearer " + "q" * 20
    )
    assert "sk-" + "a" * 30 not in redact_text("key sk-" + "a" * 30)


# --------------------------------------------------------------------------- #
# ABC seam
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_store_is_only_reachable_through_the_abc() -> None:
    assert issubclass(InMemoryDraftStore, DraftStore)


@pytest.mark.unit
async def test_abstract_methods_cannot_be_skipped() -> None:
    class HalfStore(DraftStore):
        async def save_draft(self, definition):
            ...

    with pytest.raises(TypeError):
        HalfStore()


@pytest.mark.unit
async def test_store_concurrent_reads_stay_consistent() -> None:
    """Async surface reads cleanly under concurrent callers."""
    store = InMemoryDraftStore()
    await store.save_draft(_defn())
    results = await asyncio.gather(
        *[store.get("wf-draft", 1) for _ in range(16)]
    )
    assert all(r.number == 1 for r in results)
    assert all(verify_revision(r.revision) for r in results)
