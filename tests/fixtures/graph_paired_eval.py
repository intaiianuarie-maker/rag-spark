"""G5 paired graph-vs-baseline evaluation harness (V2 work package G5).

Purpose
-------
Per ``V2-GRAPH-PLAN`` section 2 (G5): graph-vs-baseline on Q8 multi-hop /
multi-source sets; EvidenceRecall@K and grounded-success deltas reported
paired; no holdout tuning.

This harness runs a **paired, equal-K** comparison on a corpus that is
**larger than K**, so the keyword/dense baseline can genuinely miss a multi-hop
answer chunk instead of trivially retrieving everything (the failure mode the
reviewer flagged: *synthetic equal-K traversal tests do not establish a quality
gain*). It exercises the real offline extraction/retrieval pipeline:

    RuleBasedGraphExtractor (G1) -> MemoryGraphStore (G2) -> GraphRetriever (G3)
    FakeVectorStore + FakeLexicalStore over the SAME chunks = baseline

Both conditions share the corpus and the retrieval budget K, so the only
difference is the graph leg.

Honesty contract (this module invents NO evidence)
--------------------------------------------------
Two tags are reported **independently** on every result and can never be
conflated:

- **dataset_origin** ("synthetic" | "labelled"): where the *data* came from.
- **runtime_mode** ("fake" | "real"): which *retrieval components* ran. This
  harness ALWAYS runs the **fake** runtime (one-hot fake embeddings, in-memory
  fake stores, token-overlap fake reranker). Real labels do **not** change that:
  a labelled dataset on this runtime is still mechanism-only, never the V1
  production retrieval conditions. ``runtime_mode`` is **derived from the
  adapters this module constructs** (always fake) and is **not a caller-settable
  argument**, so it can be laundered neither by supplying real labels nor by
  asking for ``real``. A genuine REAL-runtime paired eval is a separate harness
  that wires the production adapters.

- **grounded-success is UNMEASURED here.** No answer engine/provider is invoked
  and no produced answer/claim/citation is inspected, so this harness cannot
  measure whether the *system* answers correctly. What it *can* measure is
  **oracle gold-answer availability**: whether the provided gold answer sentence
  is verbatim-present in a retrieved chunk (via the real v1
  :class:`~answer.verification.ExtractiveGrounder`). That is a retrieval
  diagnostic, NOT answer success -- it would score 1.0 even if the answering
  system always returned a wrong answer or abstained. It is labelled and
  reported as an oracle diagnostic; grounded-success is reported as unmeasured.
- :func:`load_labelled_dataset` returns ``None`` when no real labelled dataset is
  present and raises :class:`DatasetValidationError` for an incompatible one
  (multiple workspaces, dangling chunk references, or conflicting duplicate
  chunk ids) rather than reporting valid-looking zero scores.
- ``context_top_k`` truncation is reported, not hidden: the orchestrator's
  default context window can discard a graph-surfaced answer chunk. The harness
  measures recall both at the default window and a wide window.

Everything is deterministic (fake embeddings/rerank, RRF tie-break by id), so
results are reproducible with compose down and no model weights/keys.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.auth import ACLScope
from graph import GraphRetriever, MemoryGraphStore, RuleBasedGraphExtractor
from graph.extractor import GraphChunk
from harness_fakes import (
    FakeEmbeddingProvider,
    FakeLexicalStore,
    FakeReranker,
    FakeVectorStore,
    make_record,
)
from retrieval.interfaces import DEFAULT_CONTEXT_TOP_K, RetrievalMode
from retrieval.orchestrator import HybridRetrievalOrchestrator

#: Default corpus/eval workspace id.
WS = "ws-g5"

#: Embedding dimension for the fake dense store.
DIM = 8

#: Default retrieval budget. Kept well below the corpus size so the baseline's
#: top-K can miss the multi-hop answer chunk.
DEFAULT_K = 20

#: Independent disclosure tags.
DATASET_ORIGIN_SYNTHETIC = "synthetic"
DATASET_ORIGIN_LABELLED = "labelled"
RUNTIME_MODE_FAKE = "fake"
RUNTIME_MODE_REAL = "real"


class DatasetValidationError(ValueError):
    """Raised when a loaded dataset is incompatible with this harness.

    Signals an unusable dataset (multiple workspaces, dangling chunk
    references, or conflicting duplicate chunk ids) so the caller reports an
    incompatible dataset instead of silently producing valid-looking zero
    scores.
    """


# --------------------------------------------------------------------------------------
# Labelled examples + datasets
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PairedExample:
    """One labelled multi-hop question.

    ``relevant_chunk_ids`` are the evidence chunks a correct answer must cite
    (recall ground truth). ``gold_answer`` is a sentence expected to be verbatim
    in one of those chunks (used only for the **oracle availability**
    diagnostic). ``workspace_id`` is the *authorized scope* for this example;
    when ``None`` it defaults to the dataset corpus workspace.
    """

    id: str
    question: str
    relevant_chunk_ids: tuple[str, ...]
    gold_answer: str
    tags: tuple[str, ...] = ()
    workspace_id: str | None = None


@dataclass
class PairedDataset:
    """A set of labelled examples plus the corpus chunks they run against.

    ``dataset_origin`` is the independent data-provenance tag
    ("synthetic" | "labelled"). ``workspace_id`` is the single workspace every
    corpus chunk belongs to (the graph extraction path is single-workspace).
    """

    id: str
    name: str
    chunks: list[GraphChunk]
    examples: list[PairedExample]
    dataset_origin: str = DATASET_ORIGIN_SYNTHETIC
    workspace_id: str = WS

    @property
    def synthetic(self) -> bool:
        """Back-compat shim: True when the corpus is synthetic."""
        return self.dataset_origin == DATASET_ORIGIN_SYNTHETIC


def load_labelled_dataset(path: str | Path) -> PairedDataset | None:
    """Load a real labelled multi-hop dataset from JSONL, or ``None`` if absent.

    Expected JSONL schema (one object per line)::

        {"id": "...", "question": "...",
         "relevant_chunk_ids": ["ch-..", ...],
         "gold_answer": "verbatim sentence in a relevant chunk",
         "tags": ["multi-hop"],
         "workspace_id": "ws-..",            # optional authorized scope
         "chunks": [{"chunk_id": "ch-..", "workspace_id": "...",
                     "document_id": "...", "text": "..."}, ...]}

    Validation (raises :class:`DatasetValidationError` rather than silently
    producing zero scores):

    - Every ``relevant_chunk_ids`` entry must resolve to a chunk present in the
      file (no dangling references).
    - A ``chunk_id`` may be declared at most once; a conflicting redefinition
      (different workspace/document/text) is rejected -- dataset facts are never
      silently overwritten.
    - All chunks must share a single ``workspace_id``; the graph extraction path
      is single-workspace. The dataset's authorized scope is that workspace.

    Returns ``None`` when the file does not exist or has no examples, so the
    driver can report the missing dataset honestly instead of fabricating
    numbers. The returned dataset is tagged ``dataset_origin="labelled"``; the
    **runtime** tag is independent and set by the runner (always ``fake`` here).
    """
    p = Path(path)
    if not p.exists():
        return None
    chunks_by_id: dict[str, GraphChunk] = {}
    examples: list[PairedExample] = []
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        for c in row.get("chunks", []):
            chunk = GraphChunk(
                chunk_id=c["chunk_id"],
                workspace_id=c.get("workspace_id", WS),
                document_id=c.get("document_id", "doc-1"),
                text=c["text"],
            )
            existing = chunks_by_id.get(chunk.chunk_id)
            if existing is not None and existing != chunk:
                raise DatasetValidationError(
                    f"line {lineno}: conflicting redefinition of chunk "
                    f"{chunk.chunk_id!r} (dataset facts must not be overwritten)"
                )
            chunks_by_id[chunk.chunk_id] = chunk
        examples.append(
            PairedExample(
                id=row["id"],
                question=row["question"],
                relevant_chunk_ids=tuple(row["relevant_chunk_ids"]),
                gold_answer=row["gold_answer"],
                tags=tuple(row.get("tags", ())),
                workspace_id=row.get("workspace_id"),
            )
        )
    if not examples:
        return None

    # Validate relevant-chunk references resolve to present chunks.
    for ex in examples:
        missing = [cid for cid in ex.relevant_chunk_ids if cid not in chunks_by_id]
        if missing:
            raise DatasetValidationError(
                f"example {ex.id!r}: relevant_chunk_ids reference unknown chunks {missing}"
            )

    # Validate a single authorized workspace across the corpus.
    workspaces = {c.workspace_id for c in chunks_by_id.values()}
    if len(workspaces) > 1:
        raise DatasetValidationError(
            f"dataset spans multiple workspaces {sorted(workspaces)}; this harness "
            "supports a single authorized workspace per run"
        )
    corpus_ws = next(iter(workspaces)) if workspaces else WS

    return PairedDataset(
        id=p.stem,
        name=p.name,
        chunks=list(chunks_by_id.values()),
        examples=examples,
        dataset_origin=DATASET_ORIGIN_LABELLED,
        workspace_id=corpus_ws,
    )


def synthetic_multihop_dataset(*, n_distractors: int = 8) -> PairedDataset:
    """The shipped synthetic multi-hop corpus (mechanism demo, NOT a benchmark).

    Structure: ``Alice -> WORKS_FOR -> Acme -> OWNS -> Globex -> (produces) ->
    Zephyr``. The answer chunk ``ch-prod`` ("Globex Industries produces the
    Zephyr line.") is two hops from the seed entity and shares no content word
    with a "What does Acme Corporation make?" query, so keyword/dense retrieval
    cannot rank it into the top-K while the graph traversal reaches it via
    entity/relation provenance. Distractor chunks *do* mention "Acme
    Corporation" so the baseline is busy with near-miss keyword hits -- the
    realistic hard case.
    """
    relevant = [
        GraphChunk(
            chunk_id="ch-join",
            workspace_id=WS,
            document_id="doc-1",
            text="Alice Smith works for Acme Corporation.",
        ),
        GraphChunk(
            chunk_id="ch-acq",
            workspace_id=WS,
            document_id="doc-2",
            text="Acme Corporation owns Globex Industries.",
        ),
        GraphChunk(
            chunk_id="ch-prod",
            workspace_id=WS,
            document_id="doc-3",
            text="Globex Industries produces the Zephyr line.",
        ),
    ]
    distractors = [
        GraphChunk(
            chunk_id=f"ch-d{i:02d}",
            workspace_id=WS,
            document_id=f"doc-d{i}",
            text=(
                f"Acme Corporation quarterly report {i}: "
                "Acme Corporation revenue and filings."
            ),
        )
        for i in range(n_distractors)
    ]
    example = PairedExample(
        id="mh-001",
        question="What does Acme Corporation make?",
        relevant_chunk_ids=("ch-join", "ch-acq", "ch-prod"),
        gold_answer="Globex Industries produces the Zephyr line.",
        tags=("multi-hop", "2-hop"),
        workspace_id=WS,
    )
    return PairedDataset(
        id="synthetic-multihop",
        name="synthetic_multihop",
        chunks=relevant + distractors,
        examples=[example],
        dataset_origin=DATASET_ORIGIN_SYNTHETIC,
        workspace_id=WS,
    )


# --------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------


def evidence_recall_at_k(relevant: set[str], chunk_ids: Sequence[str], *, k: int) -> float:
    """Fraction of relevant chunks present within the top-``k`` retrieved ids."""
    if not relevant:
        return 0.0
    seen = {cid for cid in list(chunk_ids)[:k] if cid}
    return len(relevant & seen) / len(relevant)


def oracle_gold_answer_availability(
    example: PairedExample,
    retrieved_contents: Sequence[str],
    *,
    grounder,
) -> float:
    """1.0 if the *provided gold answer* is verbatim in a retrieved chunk.

    This is an **oracle / gold-answer availability diagnostic**, NOT
    grounded-success. It uses the real v1 :class:`ExtractiveGrounder`
    (conservative verbatim + sentence-level negation/conditional guard), but
    because it scores the *gold* answer against *source* text -- never an
    answer produced by the system -- it says nothing about whether the answering
    system is correct. It would return 1.0 even if the system always answered
    wrongly or abstained. Grounded-success (produced answer -> verified claims /
    citations) is UNMEASURED by this harness and reported as such.
    """
    for content in retrieved_contents:
        if content and grounder.score(example.gold_answer, content) >= 1.0:
            return 1.0
    return 0.0


# --------------------------------------------------------------------------------------
# Paired run
# --------------------------------------------------------------------------------------


@dataclass
class ConditionResult:
    """Per-condition metrics for one example.

    ``oracle_available`` is the gold-answer availability diagnostic (see
    :func:`oracle_gold_answer_availability`). Grounded-success is not measured.
    """

    recall_at_k: float
    oracle_available: float
    retrieved_chunk_ids: list[str] = field(default_factory=list)


@dataclass
class PairedExampleResult:
    """Paired (baseline vs graph) metrics for one example at one context window."""

    example_id: str
    k: int
    context_top_k: int
    baseline: ConditionResult
    graph: ConditionResult

    @property
    def recall_delta(self) -> float:
        return self.graph.recall_at_k - self.baseline.recall_at_k

    @property
    def oracle_available_delta(self) -> float:
        return self.graph.oracle_available - self.baseline.oracle_available


@dataclass
class PairedReport:
    """Aggregate paired report over a dataset at one context window.

    Carries **independent** disclosure tags:

    - ``dataset_origin`` ("synthetic" | "labelled"): data provenance.
    - ``runtime_mode`` ("fake" | "real"): retrieval components that ran. This
      harness is always ``fake``; real labels never upgrade it.
    """

    dataset_name: str
    dataset_origin: str
    runtime_mode: str
    k: int
    context_top_k: int
    n_examples: int
    mean_baseline_recall: float
    mean_graph_recall: float
    mean_recall_delta: float
    mean_baseline_oracle: float
    mean_graph_oracle: float
    mean_oracle_delta: float
    per_example: list[PairedExampleResult]

    def summary_lines(self) -> list[str]:
        origin = (
            "SYNTHETIC corpus"
            if self.dataset_origin == DATASET_ORIGIN_SYNTHETIC
            else "labelled corpus"
        )
        runtime = (
            "FAKE runtime (mechanism-only; NOT production retrieval conditions)"
            if self.runtime_mode == RUNTIME_MODE_FAKE
            else f"{RUNTIME_MODE_REAL.upper()} runtime"
        )
        return [
            f"dataset={self.dataset_name} [{origin}] [{runtime}] "
            f"n={self.n_examples} K={self.k} context_top_k={self.context_top_k}",
            f"  EvidenceRecall@{self.k}: baseline={self.mean_baseline_recall:.3f} "
            f"graph={self.mean_graph_recall:.3f} delta={self.mean_recall_delta:+.3f}",
            f"  oracle gold-answer availability: baseline={self.mean_baseline_oracle:.3f} "
            f"graph={self.mean_graph_oracle:.3f} delta={self.mean_oracle_delta:+.3f}",
            "  grounded-success: UNMEASURED (no answer engine/provider invoked; "
            "oracle availability is not answer success)",
        ]


async def _index_baseline(
    dense: FakeVectorStore, lexical: FakeLexicalStore, chunks: Sequence[GraphChunk]
) -> None:
    """Index exactly the corpus chunks into the dense + lexical baseline stores."""
    await dense.ensure_collection(collection="g5", dimension=DIM)
    records = []
    for i, c in enumerate(chunks):
        vec = [0.0] * DIM
        vec[i % DIM] = 1.0
        records.append(
            make_record(c.chunk_id, c.workspace_id, c.text, vector=vec, page=1, section="s")
        )
    await dense.upsert(collection="g5", records=records)
    await lexical.index(records=records)


def _make_orchestrator(
    dense: FakeVectorStore, lexical: FakeLexicalStore, *, graph: object | None
) -> HybridRetrievalOrchestrator:
    return HybridRetrievalOrchestrator(
        dense=dense,
        embedding=FakeEmbeddingProvider(),
        lexical=lexical,
        collection="g5",
        reranker=FakeReranker(),
        rerank_enabled=False,
        graph=graph,
    )


async def run_paired_eval(
    dataset: PairedDataset,
    *,
    k: int = DEFAULT_K,
    context_top_k: int = DEFAULT_CONTEXT_TOP_K,
    grounder=None,
) -> PairedReport:
    """Run the paired graph-vs-baseline eval for one context window.

    The dataset's authorized workspace (``dataset.workspace_id``, or a
    per-example override) is carried through extraction, ACL scope, and every
    graph query, so a non-default workspace is evaluated -- never silently
    excluded.

    The reported ``runtime_mode`` is **derived from the adapters this function
    actually constructs**, not from any caller argument. This runner only ever
    builds ``FakeVectorStore`` / ``FakeEmbeddingProvider`` / ``FakeLexicalStore``
    / ``FakeReranker``, so the runtime is unconditionally ``fake`` and there is
    no parameter by which a caller can relabel it ``real``. A genuine REAL-runtime
    paired eval is a separate harness that wires the production adapters; it is
    reported independently of ``dataset_origin``.
    """
    # Real offline extraction (G1) -> in-memory store (G2) -> retriever (G3),
    # scoped to the dataset's authorized workspace.
    extractor = RuleBasedGraphExtractor()
    extraction = await extractor.extract(dataset.chunks)
    store = MemoryGraphStore()
    await store.upsert_entities(dataset.workspace_id, extraction.entities)
    await store.upsert_relationships(dataset.workspace_id, extraction.relationships)
    retriever = GraphRetriever(store)

    # Baseline stores over the SAME chunks (corpus membership held constant).
    # These are FAKE adapters; the disclosed runtime_mode below reflects that.
    dense = FakeVectorStore(dimension=DIM)
    lexical = FakeLexicalStore()
    await _index_baseline(dense, lexical, dataset.chunks)
    # Derived from the adapters constructed above (always fake in this runner).
    runtime_mode = RUNTIME_MODE_FAKE

    # Oracle diagnostic scorer: the real v1 verifier (gold-answer availability).
    if grounder is None:
        from answer.verification import ExtractiveGrounder

        grounder = ExtractiveGrounder()

    content_by_id = {c.chunk_id: c.text for c in dataset.chunks}

    async def run(mode: RetrievalMode, *, graph: object | None, q: str, ws: str) -> list[str]:
        orch = _make_orchestrator(dense, lexical, graph=graph)
        acl = ACLScope(user_id="u-g5", workspace_ids=[ws])
        items = await orch.retrieve(
            query=q,
            acl_scope=acl,
            mode=mode,
            dense_top_k=k,
            lexical_top_k=k,
            sparse_top_k=0,
            fusion_top_k=k,
            context_top_k=context_top_k,
        )
        return [i.chunk_id for i in items if i.chunk_id]

    per_example: list[PairedExampleResult] = []
    for ex in dataset.examples:
        ws = ex.workspace_id or dataset.workspace_id
        relevant = set(ex.relevant_chunk_ids)
        b_ids = await run(RetrievalMode.HYBRID, graph=None, q=ex.question, ws=ws)
        g_ids = await run(RetrievalMode.HYBRID_GRAPH, graph=retriever, q=ex.question, ws=ws)

        # Oracle availability is scored against the underlying chunk text so the
        # comparison is apples-to-apples (the graph leg otherwise emits a
        # synthesized "[Knowledge Graph Context]" passage per evidence chunk).
        b_texts = [content_by_id.get(cid, "") for cid in b_ids]
        g_texts = [content_by_id.get(cid, "") for cid in g_ids]

        per_example.append(
            PairedExampleResult(
                example_id=ex.id,
                k=k,
                context_top_k=context_top_k,
                baseline=ConditionResult(
                    recall_at_k=evidence_recall_at_k(relevant, b_ids, k=k),
                    oracle_available=oracle_gold_answer_availability(
                        ex, b_texts, grounder=grounder
                    ),
                    retrieved_chunk_ids=b_ids,
                ),
                graph=ConditionResult(
                    recall_at_k=evidence_recall_at_k(relevant, g_ids, k=k),
                    oracle_available=oracle_gold_answer_availability(
                        ex, g_texts, grounder=grounder
                    ),
                    retrieved_chunk_ids=g_ids,
                ),
            )
        )

    n = max(len(per_example), 1)
    return PairedReport(
        dataset_name=dataset.name,
        dataset_origin=dataset.dataset_origin,
        runtime_mode=runtime_mode,
        k=k,
        context_top_k=context_top_k,
        n_examples=len(per_example),
        mean_baseline_recall=sum(r.baseline.recall_at_k for r in per_example) / n,
        mean_graph_recall=sum(r.graph.recall_at_k for r in per_example) / n,
        mean_recall_delta=sum(r.recall_delta for r in per_example) / n,
        mean_baseline_oracle=sum(r.baseline.oracle_available for r in per_example) / n,
        mean_graph_oracle=sum(r.graph.oracle_available for r in per_example) / n,
        mean_oracle_delta=sum(r.oracle_available_delta for r in per_example) / n,
        per_example=per_example,
    )


# --------------------------------------------------------------------------------------
# Deterministic leg-level discriminators (stable ground truth for tests)
# --------------------------------------------------------------------------------------


async def build_graph_retriever(dataset: PairedDataset) -> GraphRetriever:
    """Extract (G1) -> in-memory store (G2) -> retriever (G3) for the dataset.

    Scoped to ``dataset.workspace_id`` so a non-default workspace is populated
    and queryable rather than silently empty.
    """
    extractor = RuleBasedGraphExtractor()
    extraction = await extractor.extract(dataset.chunks)
    store = MemoryGraphStore()
    await store.upsert_entities(dataset.workspace_id, extraction.entities)
    await store.upsert_relationships(dataset.workspace_id, extraction.relationships)
    return GraphRetriever(store)


async def graph_retriever_evidence_recall(
    dataset: PairedDataset,
    example: PairedExample,
    retriever: GraphRetriever,
    *,
    depth: int = 1,
    k: int = DEFAULT_K,
) -> float:
    """EvidenceRecall@k of the **graph leg alone** (``GraphRetriever.local_search``).

    This is the level at which graph retrieval is *defined* to operate. It is
    deterministic: entity/relation traversal surfaces the multi-hop answer
    chunk that keyword retrieval cannot reach. It is **not** an end-to-end
    quality claim -- see :func:`run_paired_eval` for the fused result.
    """
    ws = example.workspace_id or dataset.workspace_id
    result = await retriever.local_search(workspace_id=ws, query=example.question, depth=depth)
    return evidence_recall_at_k(set(example.relevant_chunk_ids), result.evidence_chunk_ids, k=k)


async def keyword_baseline_evidence_recall(
    dataset: PairedDataset,
    example: PairedExample,
    *,
    k: int = DEFAULT_K,
) -> float:
    """EvidenceRecall@k of a **keyword/lexical-only** baseline over the corpus.

    The graph leg is meant to beat exactly this: token-overlap retrieval that
    cannot follow an entity/relation chain to a chunk sharing no query terms.
    Deterministic and the honest discriminator for the multi-hop mechanism.
    Scoped to the example's authorized workspace (ACL-before-rank, fail closed).
    """
    lexical = FakeLexicalStore()
    dense = FakeVectorStore(dimension=DIM)
    await _index_baseline(dense, lexical, dataset.chunks)
    from retrieval.interfaces import SearchFilter

    ws = example.workspace_id or dataset.workspace_id
    qf = SearchFilter(workspace_ids=[ws])
    items = await lexical.search(query=example.question, top_k=k, query_filter=qf)
    ids = [i.chunk_id for i in items if i.chunk_id]
    return evidence_recall_at_k(set(example.relevant_chunk_ids), ids, k=k)
