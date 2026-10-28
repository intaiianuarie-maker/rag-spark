"""G5 paired graph-vs-baseline evaluation tests (V2 work package G5).

Locks the corrected honesty contract from ``tests/fixtures/graph_paired_eval.py``
(review V2-G4-G5-PREP-REVIEW-1 P2-1/2/3):

- The **graph leg** surfaces a 2-hop multi-hop answer chunk a **keyword
  baseline** cannot reach (the *mechanism*).
- **Oracle gold-answer availability** (real v1 ``ExtractiveGrounder`` on the
  *gold* answer) is a retrieval diagnostic, NOT grounded-success. Grounded-
  success is reported **UNMEASURED** (no answer engine/provider is invoked).
- The dataset's **authorized workspace** is carried through extraction, ACL, and
  graph queries: a non-default workspace is evaluated (1/1), never silently
  excluded (0/0). Out-of-scope queries fail closed.
- **dataset_origin** and **runtime_mode** are independent tags; the runtime is
  always ``fake`` here and real labels cannot launder it.
- Loader validation rejects dangling chunk refs, conflicting duplicate chunk
  ids, and multi-workspace corpora (raises, not zero scores).

Deterministic and hermetic (fake embeddings/rerank; no services, weights, keys).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from answer.verification import ExtractiveGrounder

# T-I05: two modules are named ``graph_paired_eval`` — this test's fixture helper
# (``tests/fixtures/graph_paired_eval.py``) and the CLI driver of the same name
# (``evals/graph_paired_eval.py``). A bare top-level import resolves to whichever
# copy comes first on sys.path, and under the full-suite run that was the driver,
# which does not export these symbols — breaking collection for the whole suite.
#
# The helper is therefore loaded from its real path under a private alias, so the
# test is immune to sys.path ordering and to any other module mutating it.
_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "graph_paired_eval.py"
)
_spec = importlib.util.spec_from_file_location("_g5_fixture_graph_paired_eval", _FIXTURE_PATH)
assert _spec and _spec.loader, f"fixture helper not found at {_FIXTURE_PATH}"
_fixture = importlib.util.module_from_spec(_spec)
sys.modules["_g5_fixture_graph_paired_eval"] = _fixture
_spec.loader.exec_module(_fixture)

DATASET_ORIGIN_LABELLED = _fixture.DATASET_ORIGIN_LABELLED
DATASET_ORIGIN_SYNTHETIC = _fixture.DATASET_ORIGIN_SYNTHETIC
DEFAULT_K = _fixture.DEFAULT_K
RUNTIME_MODE_FAKE = _fixture.RUNTIME_MODE_FAKE
DatasetValidationError = _fixture.DatasetValidationError
build_graph_retriever = _fixture.build_graph_retriever
evidence_recall_at_k = _fixture.evidence_recall_at_k
graph_retriever_evidence_recall = _fixture.graph_retriever_evidence_recall
keyword_baseline_evidence_recall = _fixture.keyword_baseline_evidence_recall
load_labelled_dataset = _fixture.load_labelled_dataset
oracle_gold_answer_availability = _fixture.oracle_gold_answer_availability
run_paired_eval = _fixture.run_paired_eval
synthetic_multihop_dataset = _fixture.synthetic_multihop_dataset

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------------------
# Mechanism (leg level)
# --------------------------------------------------------------------------------------


async def test_graph_leg_surfaces_multihop_answer_chunk_keyword_baseline_misses() -> None:
    """The core G5 mechanism, measured as a paired leg-level delta.

    Graph retrieval (entity/relation traversal) reaches the 2-hop answer chunk
    ``ch-prod``; the keyword/lexical baseline over the same corpus does not.
    This is a *mechanism* demonstration, not an end-to-end quality claim.
    """
    dataset = synthetic_multihop_dataset(n_distractors=8)
    example = dataset.examples[0]
    retriever = await build_graph_retriever(dataset)

    graph_recall = await graph_retriever_evidence_recall(dataset, example, retriever, k=DEFAULT_K)
    keyword_recall = await keyword_baseline_evidence_recall(dataset, example, k=DEFAULT_K)

    assert graph_recall == 1.0, "graph leg must surface the full evidence subgraph"
    assert keyword_recall < 1.0, "keyword baseline must miss at least the multi-hop chunk"
    assert graph_recall > keyword_recall, "graph leg must beat the keyword baseline here"


async def test_oracle_gold_answer_availability_is_a_diagnostic_not_success() -> None:
    """Oracle availability uses the real grounder on the GOLD answer, not a
    produced answer -- it is explicitly NOT grounded-success."""
    dataset = synthetic_multihop_dataset(n_distractors=8)
    example = dataset.examples[0]
    grounder = ExtractiveGrounder()

    # The gold answer is verbatim in ch-prod -> oracle available.
    assert (
        oracle_gold_answer_availability(
            example, ["Globex Industries produces the Zephyr line."], grounder=grounder
        )
        == 1.0
    )
    # A chunk that mentions entities but not the answer sentence -> not available.
    assert (
        oracle_gold_answer_availability(
            example, ["Acme Corporation owns Globex Industries."], grounder=grounder
        )
        == 0.0
    )


# --------------------------------------------------------------------------------------
# Workspace scope (P2-2)
# --------------------------------------------------------------------------------------


def _write_dataset(tmp_path, *, workspace_id: str, name: str = "labels.jsonl"):
    """Write a single-example labelled dataset (same corpus, given workspace)."""
    chunks = [
        {"chunk_id": "ch-join", "workspace_id": workspace_id, "document_id": "doc-1",
         "text": "Alice Smith works for Acme Corporation."},
        {"chunk_id": "ch-acq", "workspace_id": workspace_id, "document_id": "doc-2",
         "text": "Acme Corporation owns Globex Industries."},
        {"chunk_id": "ch-prod", "workspace_id": workspace_id, "document_id": "doc-3",
         "text": "Globex Industries produces the Zephyr line."},
    ]
    # Add keyword near-miss distractors so the corpus is larger than K.
    for i in range(8):
        chunks.append(
            {"chunk_id": f"ch-d{i:02d}", "workspace_id": workspace_id,
             "document_id": f"doc-d{i}",
             "text": f"Acme Corporation quarterly report {i}: Acme revenue."}
        )
    row = {
        "id": "mh-001",
        "question": "What does Acme Corporation make?",
        "relevant_chunk_ids": ["ch-join", "ch-acq", "ch-prod"],
        "gold_answer": "Globex Industries produces the Zephyr line.",
        "tags": ["multi-hop"],
        "workspace_id": workspace_id,
        "chunks": chunks,
    }
    path = tmp_path / name
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


async def test_loaded_non_default_workspace_is_evaluated_not_excluded(tmp_path) -> None:
    """P2-2 regression: a loaded non-default workspace must NOT collapse to 0/0.

    Replacing every chunk workspace with ``company-real`` and authorizing that
    workspace must keep recall at 1.0/1.0 (wide window) -- not silently 0.0/0.0
    from the harness hard-coding ``ws-g5``.
    """
    path = _write_dataset(tmp_path, workspace_id="company-real")
    dataset = load_labelled_dataset(path)
    assert dataset is not None
    assert dataset.workspace_id == "company-real"
    assert dataset.dataset_origin == DATASET_ORIGIN_LABELLED

    retriever = await build_graph_retriever(dataset)
    example = dataset.examples[0]
    graph_recall = await graph_retriever_evidence_recall(dataset, example, retriever, k=DEFAULT_K)
    assert graph_recall == 1.0, "non-default workspace graph leg must still surface evidence"

    report = await run_paired_eval(dataset, k=DEFAULT_K, context_top_k=DEFAULT_K)
    ex = report.per_example[0]
    assert ex.graph.recall_at_k == 1.0
    assert "ch-prod" in ex.graph.retrieved_chunk_ids


async def test_out_of_scope_workspace_fails_closed(tmp_path) -> None:
    """P2-2 scope-denial negative: an unauthorized workspace yields no evidence."""
    path = _write_dataset(tmp_path, workspace_id="company-real")
    dataset = load_labelled_dataset(path)
    assert dataset is not None
    retriever = await build_graph_retriever(dataset)
    example = dataset.examples[0]

    # Authorize a DIFFERENT workspace than the corpus -> graph leg sees nothing.
    from dataclasses import replace

    scoped = replace(example, workspace_id="other-ws")
    graph_recall = await graph_retriever_evidence_recall(dataset, scoped, retriever, k=DEFAULT_K)
    assert graph_recall == 0.0, "out-of-scope workspace must fail closed"

    # Force the example scope mismatch and re-run through the orchestrator path.
    mismatched = replace(dataset, examples=[scoped])
    report2 = await run_paired_eval(mismatched, k=DEFAULT_K, context_top_k=DEFAULT_K)
    assert report2.per_example[0].graph.recall_at_k == 0.0
    assert report2.per_example[0].baseline.recall_at_k == 0.0


# --------------------------------------------------------------------------------------
# Loader validation (P2-2)
# --------------------------------------------------------------------------------------


def test_loader_rejects_dangling_relevant_chunk_ref(tmp_path) -> None:
    path = tmp_path / "bad.jsonl"
    chunk = {"chunk_id": "ch-real", "workspace_id": "ws-g5", "document_id": "d", "text": "t"}
    row = {
        "id": "x", "question": "q?", "relevant_chunk_ids": ["ch-missing"],
        "gold_answer": "a", "chunks": [chunk],
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(DatasetValidationError, match="unknown chunks"):
        load_labelled_dataset(path)


def test_loader_rejects_conflicting_duplicate_chunk_id(tmp_path) -> None:
    path = tmp_path / "dup.jsonl"
    c1 = {"chunk_id": "ch-1", "workspace_id": "ws-g5", "document_id": "d", "text": "first"}
    c2 = {"chunk_id": "ch-1", "workspace_id": "ws-g5", "document_id": "d", "text": "DIFFERENT"}
    row1 = {
        "id": "x", "question": "q?", "relevant_chunk_ids": ["ch-1"],
        "gold_answer": "a", "chunks": [c1],
    }
    row2 = {
        "id": "y", "question": "q2?", "relevant_chunk_ids": ["ch-1"],
        "gold_answer": "b", "chunks": [c2],
    }
    path.write_text(json.dumps(row1) + "\n" + json.dumps(row2) + "\n", encoding="utf-8")
    with pytest.raises(DatasetValidationError, match="conflicting redefinition"):
        load_labelled_dataset(path)


def test_loader_rejects_multi_workspace_corpus(tmp_path) -> None:
    path = tmp_path / "multi.jsonl"
    row = {
        "id": "x", "question": "q?", "relevant_chunk_ids": ["ch-1"], "gold_answer": "a",
        "chunks": [
            {"chunk_id": "ch-1", "workspace_id": "ws-a", "document_id": "d", "text": "t"},
            {"chunk_id": "ch-2", "workspace_id": "ws-b", "document_id": "d", "text": "u"},
        ],
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(DatasetValidationError, match="multiple workspaces"):
        load_labelled_dataset(path)


def test_loader_returns_none_when_missing(tmp_path) -> None:
    assert load_labelled_dataset(tmp_path / "does-not-exist.jsonl") is None


# --------------------------------------------------------------------------------------
# Independent disclosure tags (P2-3)
# --------------------------------------------------------------------------------------


async def test_runtime_disclosure_not_removable_by_labels(tmp_path) -> None:
    """P2-3 regression: real labels do NOT upgrade the runtime disclosure.

    A loaded (labelled) dataset still runs the FAKE runtime, and every report
    line must say so. Grounded-success stays UNMEASURED regardless of labels.
    """
    path = _write_dataset(tmp_path, workspace_id="company-real")
    dataset = load_labelled_dataset(path)
    assert dataset is not None
    assert dataset.dataset_origin == DATASET_ORIGIN_LABELLED

    report = await run_paired_eval(dataset, k=DEFAULT_K, context_top_k=DEFAULT_K)
    # Origin is labelled, but runtime is STILL fake (set by the harness).
    assert report.dataset_origin == DATASET_ORIGIN_LABELLED
    assert report.runtime_mode == RUNTIME_MODE_FAKE

    lines = report.summary_lines()
    joined = "\n".join(lines)
    assert "labelled corpus" in joined
    assert "FAKE runtime" in joined
    assert "mechanism-only" in joined
    assert "grounded-success: UNMEASURED" in joined


async def test_caller_cannot_request_real_runtime() -> None:
    """P2 regression: no caller argument can relabel the fake runtime as REAL.

    ``run_paired_eval`` builds only fake adapters, so the disclosed
    ``runtime_mode`` must be ``fake`` regardless of any attempt to request
    ``real``. The override parameter was removed; passing it must be a
    ``TypeError``, and the report/summary can never say REAL for this runner.
    """
    dataset = synthetic_multihop_dataset(n_distractors=8)

    # The override no longer exists on the fake runner.
    with pytest.raises(TypeError):
        await run_paired_eval(dataset, k=DEFAULT_K, runtime_mode="real")  # type: ignore[call-arg]

    # And the only supported invocation is unconditionally fake.
    report = await run_paired_eval(dataset, k=DEFAULT_K, context_top_k=DEFAULT_K)
    assert report.runtime_mode == RUNTIME_MODE_FAKE
    joined = "\n".join(report.summary_lines())
    assert "REAL runtime" not in joined
    assert "FAKE runtime" in joined


async def test_synthetic_report_tags_both_dimensions() -> None:
    """Synthetic corpus + fake runtime are both disclosed on every report."""
    dataset = synthetic_multihop_dataset(n_distractors=8)
    report = await run_paired_eval(dataset, k=DEFAULT_K, context_top_k=6)
    assert report.dataset_origin == DATASET_ORIGIN_SYNTHETIC
    assert report.runtime_mode == RUNTIME_MODE_FAKE
    joined = "\n".join(report.summary_lines())
    assert "SYNTHETIC corpus" in joined
    assert "FAKE runtime" in joined
    assert "grounded-success: UNMEASURED" in joined


# --------------------------------------------------------------------------------------
# Fused report honesty + invariants
# --------------------------------------------------------------------------------------


async def test_fused_orchestrator_reports_paired_deltas_without_claiming_gain() -> None:
    """The fused HYBRID_GRAPH result is reported, but no gain is asserted.

    Documents the honest limitation: with the graph leg's uniform item scores
    under RRF and the default ``context_top_k`` window, the multi-hop advantage
    can be truncated or diluted. The report must be internally consistent and
    tag BOTH dataset origin and runtime mode.
    """
    dataset = synthetic_multihop_dataset(n_distractors=8)
    report = await run_paired_eval(dataset, k=DEFAULT_K, context_top_k=6)

    assert report.n_examples == 1
    ex = report.per_example[0]
    assert ex.recall_delta == pytest.approx(ex.graph.recall_at_k - ex.baseline.recall_at_k)
    assert ex.oracle_available_delta == pytest.approx(
        ex.graph.oracle_available - ex.baseline.oracle_available
    )
    assert report.mean_graph_recall == pytest.approx(ex.graph.recall_at_k)
    assert report.mean_baseline_recall == pytest.approx(ex.baseline.recall_at_k)


async def test_wide_context_window_recovers_graph_evidence_in_fusion() -> None:
    """With a wide context window the fused path retains graph evidence.

    At ``context_top_k == k`` (no truncation) the fused HYBRID_GRAPH path keeps
    the full evidence set, matching the retriever-level recall. This shows the
    neutral default result is a *context-window truncation* effect, not a graph
    retrieval failure -- reported honestly, still not a production quality claim.
    """
    dataset = synthetic_multihop_dataset(n_distractors=8)
    report = await run_paired_eval(dataset, k=DEFAULT_K, context_top_k=DEFAULT_K)
    ex = report.per_example[0]
    assert "ch-prod" in ex.graph.retrieved_chunk_ids
    assert ex.graph.recall_at_k == 1.0


def test_evidence_recall_at_k_edges() -> None:
    assert evidence_recall_at_k(set(), ["a"], k=5) == 0.0
    assert evidence_recall_at_k({"a", "b"}, ["a", "x", "b"], k=2) == 0.5  # 'b' outside top-2
    assert evidence_recall_at_k({"a", "b"}, ["a", "b"], k=2) == 1.0
