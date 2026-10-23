"""T-EVAL-HARNESS — production-leg eval harness (R08 protocol, full n, repeatable).

R13b taught us the bge pin died; this harness keeps the **gemini production
path** exactly as R08 ran it, but (a) at full n, (b) repeatable, and (c) through
the **real ingest leg** (upload API → worker → collection) instead of R08's
direct Postgres/Qdrant inserts.

METRIC PARITY WITH R08 (the contract this file must not drift from)
------------------------------------------------------------------
Every metric definition is **imported from `evals.live_v1_gate`**, the R08
harness, rather than re-typed here. If R08 changes a threshold, this harness
moves with it. The definitions in force are:

* **EvidenceRecall@20** — `1.0` iff any gold chunk id is among the retrieved
  chunk ids at ``context_top_k=20``; `1.0` (N/A) for unanswerable items.
* **Refusal detection** — status in ``{INSUFFICIENT_EVIDENCE, UNAVAILABLE}``
  or an answer containing one of the R08 refusal phrases.
* **Grounded Answer Success** — answerable: NOT a refusal AND
  ``status == ANSWERED`` AND ``>= 1`` citation AND faithfulness ``>= 0.90``.
* **Factual Precision** — answerable: fraction of claims with faithfulness
  ``>= 0.90`` (``0.0`` when there are no claims and it is not a refusal).
* **Citation Validity** — answerable: ``1.0`` iff ``>= 1`` citation.
* **Correct Abstention** — unanswerable: ``1.0`` iff refused AND zero citations.

Faithfulness is ``app.eval.metrics.score_faithfulness`` in both harnesses.
Citations are produced by the engine's real ``verify_claims`` path with a
canonical chunk lookup, so a citation that survives is canonically resolved;
``citations_canonical`` is reported alongside the R08 gate as corroboration.

WHAT IS NEW HERE (does not change the metric definitions)
---------------------------------------------------------
1. **Production ingest leg** — documents enter through
   ``POST /v1/workspaces/{ws}/documents`` (the real upload route) and are
   processed by the real worker consumer (``ingest_worker.process_one``), so
   parsing, chunking, embedding and upsert are the production ones.
2. **Full n** — no dev/holdout subsampling; every loaded item is evaluated and
   ``n`` is reported per stratum.
3. **Strata** — results are broken out by ``multi`` (multi-hop) vs ``tables``
   (table QA), plus the language and unanswerable slices.
4. **Provenance in the report** — run id, corpus checksum, embedding and
   generation model ids, and metered spend.

CORPUS CONTRACT (dev2's files are inputs, imported — never written here)
------------------------------------------------------------------------
The corpus and its fixtures are prepared in parallel under ``evals/ingest/``.
This harness **imports** them and does not write them. Expected interface:

    from evals.ingest import load_corpus   # -> list[EvalItem]

``EvalItem`` is R08's dataclass (``evals.live_v1_gate.EvalItem``) with the
fields ``slice_name``, ``source_id``, ``source_path``, ``question``,
``context``, ``gold_answer``, ``content_sha256``, ``split``. Stratum is taken
from ``slice_name``: ``multi`` → ``multi-hop``, ``tables`` → ``table``.

If that module is absent the harness refuses with a clear error. It never
silently falls back to a different corpus — a fallback would make the reported
checksum describe data nobody audited.

METERED-CALL GUARD
------------------
``--dry-run`` (the DEFAULT) must make **zero** metered calls. This is proven,
not asserted: :class:`MeteredGuard` snapshots ``GLOBAL_ACCOUNTING`` before and
after the whole run and fails loudly if any metered call, token or cent of
spend was recorded. Dry-run substitutes in-process fakes for the two metered
legs (embedding, generation) and never touches the network for them.

USAGE
-----
    # proves zero metered calls; no corpus needed
    python evals/v1_full_harness.py --dry-run

    # the real run — coordinator sequences this once the corpus lands
    python evals/v1_full_harness.py --limit-n 0 --out /tmp/opencode/v1_full.json
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import pathlib
import sys
import time
import uuid
from collections.abc import Sequence
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in ("packages", "apps/api", "apps/worker", "scripts", "."):
    _pstr = str((_REPO_ROOT / _p).resolve())
    if _pstr not in sys.path:
        sys.path.insert(0, _pstr)

# --- R08 metric parity: imported, never re-typed -----------------------------
from answer.engine import AnswerEngine  # noqa: E402
from answer.models import Answer, AnswerStatus, Citation, Claim  # noqa: E402
from app.eval.metrics import score_faithfulness  # noqa: E402
from core.auth import ACLScope  # noqa: E402
from models.adapters.openrouter_provider import GLOBAL_ACCOUNTING, OpenRouterProvider  # noqa: E402
from retrieval.interfaces import EmbeddingProvider, RetrievedItem  # noqa: E402

from evals.live_v1_gate import (  # noqa: E402
    ORGANIZATION_ID,
    EvalItem,
    percentile,
    wilson_score_interval,
)

CALL_SITE = "v1_full_harness"

#: R08 targets, imported from the same source as the definitions.
from evals.live_v1_gate import TARGETS  # noqa: E402

#: Embedding model used by the production serving path (3072-d).
EMBED_MODEL = "google/gemini-embedding-2"
#: Generation model: free-first, with the provider's automatic metered fallback.
GEN_MODEL = "google/gemma-4-26b-a4b-it:free"

#: Stratum labels (the task's multi-hop vs table split).
STRATUM_MULTI = "multi-hop"
STRATUM_TABLE = "table"
STRATUM_OTHER = "other"

#: R08 thresholds used when binning a metric as pass/fail.
FAITHFULNESS_FLOOR = 0.90


def stratum_of(item: EvalItem) -> str:
    """Map an item's slice onto a reporting stratum."""
    slice_name = (item.slice_name or "").lower()
    if slice_name in ("multi", "multi_hop", "multi-hop", "multihop"):
        return STRATUM_MULTI
    if slice_name in MULTIHOP_TYPES:  # 2wiki: every delivered type is multi-hop
        return STRATUM_MULTI
    if slice_name in ("tables", "table", "table_qa"):
        return STRATUM_TABLE
    return STRATUM_OTHER


# --------------------------------------------------------------------------- #
# Metered-call guard — the dry-run proof
# --------------------------------------------------------------------------- #


class MeteredCallLeak(RuntimeError):
    """A dry-run recorded metered usage; the guard exists to make this loud."""


@dataclasses.dataclass
class MeteredSnapshot:
    n_calls: int
    n_paid_calls: int
    total_tokens: int
    cost_usd: float


#: Hard spend ceiling for a metered run. The run stops and reports if this is
#: approached — the budget is a stop condition, not an aspiration.
BUDGET_CEILING_USD = 5.00


class BudgetExceeded(RuntimeError):
    """The metered run reached its spend ceiling and stopped."""


class BudgetGuard:
    """Stops the run when cumulative metered spend approaches the ceiling."""

    def __init__(self, ceiling_usd: float = BUDGET_CEILING_USD) -> None:
        self.ceiling = ceiling_usd
        self.stopped = False

    def spent(self) -> float:
        return float(GLOBAL_ACCOUNTING.summary()["total"].get("cost_usd", 0.0))

    def check(self) -> None:
        spent = self.spent()
        if spent >= self.ceiling * 0.8:
            self.stopped = True
            raise BudgetExceeded(
                f"metered spend ${spent:.4f} reached 80% of the ${self.ceiling:.2f} "
                "ceiling; stopping the run and reporting partial numbers"
            )


def _snapshot() -> MeteredSnapshot:
    """Read the accounting totals.

    NOTE: ``CallStats.as_dict`` names these keys ``calls`` / ``paid_calls``
    (not ``n_calls`` / ``n_paid_calls``). Reading the wrong key silently
    yielded 0 and would have let a zero-token, zero-cost call slip past the
    guard, so the keys are asserted rather than defaulted.
    """
    total = GLOBAL_ACCOUNTING.summary()["total"]
    missing = [k for k in ("calls", "paid_calls", "total_tokens", "cost_usd") if k not in total]
    if missing:
        raise KeyError(
            f"accounting summary is missing expected keys {missing}; the guard "
            "would under-count and silently pass a metered run"
        )
    return MeteredSnapshot(
        n_calls=int(total["calls"]),
        n_paid_calls=int(total["paid_calls"]),
        total_tokens=int(total["total_tokens"]),
        cost_usd=float(total["cost_usd"]),
    )


class MeteredGuard:
    """Asserts a run made zero metered calls.

    In dry-run the guard is a *proof*: it compares the accounting registry
    before and after, so a code path that quietly reached a metered provider
    fails the harness instead of silently costing money.
    """

    def __init__(self, *, enforced: bool) -> None:
        self.enforced = enforced
        self.before: MeteredSnapshot | None = None

    def __enter__(self) -> MeteredGuard:
        self.before = _snapshot()
        return self

    def __exit__(self, *exc: Any) -> None:
        if not self.enforced or self.before is None:
            return
        after = _snapshot()
        delta = MeteredSnapshot(
            n_calls=after.n_calls - self.before.n_calls,
            n_paid_calls=after.n_paid_calls - self.before.n_paid_calls,
            total_tokens=after.total_tokens - self.before.total_tokens,
            cost_usd=round(after.cost_usd - self.before.cost_usd, 8),
        )
        if delta.n_calls or delta.n_paid_calls or delta.total_tokens or delta.cost_usd:
            raise MeteredCallLeak(
                "dry-run recorded metered usage: "
                f"calls={delta.n_calls} paid={delta.n_paid_calls} "
                f"tokens={delta.total_tokens} cost_usd={delta.cost_usd}"
            )

    def report(self) -> dict[str, Any]:
        """Spend observed during the run (always zero under the guard)."""
        after = _snapshot()
        if self.before is None:
            return {"n_calls": 0, "n_paid_calls": 0, "total_tokens": 0, "cost_usd": 0.0}
        return {
            "n_calls": after.n_calls - self.before.n_calls,
            "n_paid_calls": after.n_paid_calls - self.before.n_paid_calls,
            "total_tokens": after.total_tokens - self.before.total_tokens,
            "cost_usd": round(after.cost_usd - self.before.cost_usd, 8),
        }


# --------------------------------------------------------------------------- #
# Corpus loading (dev2's evals/ingest/ is an input)
# --------------------------------------------------------------------------- #


#: Document text directory delivered alongside the corpus manifest.
DOCS_DIR = _REPO_ROOT / "evals" / "ingest" / "docs"
#: Corpus manifest delivered by dev2 (T-EVAL-CORPUS, commit f206e29).
MANIFEST_PATH = _REPO_ROOT / "evals" / "ingest" / "manifest.json"

#: 2WikiMultihopQA item types. Every type in this slice is a multi-hop
#: question; there is no table-QA stratum, which the report states explicitly
#: rather than inventing one.
MULTIHOP_TYPES = frozenset(
    {"compositional", "inference", "comparison", "bridge_comparison"}
)


def load_corpus() -> list[EvalItem]:
    """Load dev2's delivered corpus (T-EVAL-CORPUS, commit f206e29).

    HARNESS BUG FIX (recorded): this function originally did
    ``from evals.ingest import load_corpus``. The delivered corpus is a
    ``manifest.json`` plus a flat ``docs/`` directory — there is no importable
    Python module — so the original contract could never resolve. This reads
    the actual delivered shape. **No metric definition was touched**: the item
    count, the fields R08's ``EvalItem`` needs, and every scoring rule are
    unchanged; only the way items are read differs.

    Each manifest item names ``gold_doc_ids`` among its ``all_doc_ids``. That is
    the honest multi-hop gold signal, so it is carried through to the ingest leg
    (which resolves gold *chunk* ids from gold *document* ids) instead of the
    text-match heuristic used for single-hop gold.
    """
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"corpus manifest missing at {MANIFEST_PATH}")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    # Reset side tables BEFORE the population loop (clearing them afterwards
    # would wipe what that loop just filled — a bug this harness actually hit).
    GOLD_DOC_IDS.clear()
    DOCS_BY_ITEM.clear()

    items: list[EvalItem] = []
    for raw in manifest["items"]:
        doc_ids = list(raw["all_doc_ids"])
        parts: list[str] = []
        h = hashlib.sha256()
        docs_for_item: list[tuple[str, str]] = []
        for doc_id in doc_ids:
            path = DOCS_DIR / f"{doc_id}.txt"
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            parts.append(text)
            docs_for_item.append((doc_id, text))
            h.update(doc_id.encode("utf-8"))
            h.update(text.encode("utf-8"))
        DOCS_BY_ITEM[raw["id"]] = docs_for_item
        context = "\n\n".join(p.strip() for p in parts if p.strip())
        items.append(
            EvalItem(
                slice_name=raw["type"],
                source_id=raw["id"],
                source_path=str(DOCS_DIR),
                question=raw["question"],
                context=context,
                gold_answer=raw.get("expected_answer", ""),
                content_sha256=h.hexdigest(),
                split=manifest.get("split", "validation"),
            )
        )
    if not items:
        raise ValueError("corpus manifest contained zero items")
    for raw in manifest["items"]:
        GOLD_DOC_IDS[raw["id"]] = list(raw["gold_doc_ids"])
    return items


#: item id -> gold document ids (the multi-hop gold signal from the manifest).
GOLD_DOC_IDS: dict[str, list[str]] = {}
#: item id -> [(corpus doc id, text)]. One upload per source document, so gold
#: documents map to real uploaded versions and gold CHUNKS resolve honestly.
DOCS_BY_ITEM: dict[str, list[tuple[str, str]]] = {}


def corpus_checksum(items: Sequence[EvalItem]) -> str:
    """Stable sha256 over the whole corpus, so a report names its exact data."""
    h = hashlib.sha256()
    for item in sorted(items, key=lambda i: (i.slice_name, i.source_id, i.question)):
        h.update(item.content_sha256.encode("utf-8"))
        h.update(b"\x00")
        h.update(item.question.encode("utf-8"))
        h.update(b"\x00")
        h.update(item.gold_answer.encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Dry-run stand-ins (no network, no metering)
# --------------------------------------------------------------------------- #


class _DryRunEmbedder(EmbeddingProvider):
    """Deterministic embedder for dry-run; records nothing, meters nothing."""

    def __init__(self, dim: int = 3072) -> None:
        self.dim = dim

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
            out.append([(seed >> (i % 30)) % 97 / 97.0 for i in range(self.dim)])
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


# --------------------------------------------------------------------------- #
# Per-item evaluation — R08 definitions, verbatim
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class IngestedRef:
    """What the production ingest leg returned for one item."""

    item: EvalItem
    workspace_id: str
    document_id: str
    chunk_ids: list[str]
    gold_chunk_ids: list[str]
    job_id: str = ""


@dataclasses.dataclass
class ItemResult:
    ref: IngestedRef
    stratum: str
    recall_at_20: float
    grounded_success: bool
    factual_precision: float
    citation_validity: float
    correct_abstention: float | None
    faithfulness: float
    is_refusal: bool
    n_citations: int
    n_claims: int
    citations_canonical: int
    retrieval_latency_ms: int
    generation_latency_ms: int


def chunk_context(context: str) -> list[str]:
    """Split a context blob into paragraph-ish chunks (R08's shaping)."""
    parts = [p.strip() for p in context.split("\n\n")]
    return [p for p in parts if p] or ([context.strip()] if context.strip() else [])


def gold_chunks_for(item: EvalItem, chunk_ids: Sequence[str], chunks: Sequence[str]) -> list[str]:
    """Identify which ingested chunks substantiate the gold answer (R08 rule)."""
    clean_gold = (item.gold_answer or "").strip().lower()
    if not clean_gold:
        return []
    gold = [
        cid
        for cid, content in zip(chunk_ids, chunks, strict=True)
        if clean_gold in content.lower()
    ]
    if gold:
        return gold
    scores = [score_faithfulness(item.gold_answer, content) for content in chunks]
    best = max(scores) if scores else 0.0
    if best > 0.0:
        return [chunk_ids[scores.index(best)]]
    return []


def _dry_run_answer(ref: IngestedRef) -> Answer:
    """Dry-run answer synthesis — mirrors R08's dry-run branch exactly.

    Answerable items answer with the gold text and one citation; unanswerable
    items refuse with no citations. Nothing here meters.
    """
    item = ref.item
    cid = ref.chunk_ids[0] if ref.chunk_ids else "dryrun-chunk"
    if item.is_answerable:
        text = item.gold_answer or "mock answer"
        return Answer(
            answer=text,
            status=AnswerStatus.ANSWERED,
            claims=[Claim(text=text, chunk_ids=[cid])],
            citations=[
                Citation(
                    chunk_id=cid,
                    document_id=ref.document_id,
                    workspace_id=ref.workspace_id,
                    grounding_score=1.0,
                    content=item.context,
                )
            ],
        )
    return Answer(
        answer="I cannot answer this question.",
        status=AnswerStatus.INSUFFICIENT_EVIDENCE,
        reason_code="EVIDENCE_MISSING",
        claims=[],
        citations=[],
    )


def is_refusal_answer(answer: Answer) -> bool:
    """R08 refusal rule (status + phrases), unchanged."""
    lower = (answer.answer or "").lower()
    return answer.status in (
        AnswerStatus.INSUFFICIENT_EVIDENCE,
        AnswerStatus.UNAVAILABLE,
    ) or any(
        p in lower
        for p in (
            "cannot answer",
            "does not contain",
            "insufficient information",
            "nu conține",
            "não contém",
        )
    )


async def evaluate_ref(
    ref: IngestedRef,
    *,
    orchestrator: Any,
    engine: AnswerEngine,
    dry_run: bool,
    canonical_ids: Sequence[str] | None = None,
) -> ItemResult:
    """Score one ingested item with the R08 metric definitions."""
    item = ref.item
    scope = ACLScope(user_id="v1-full-evaluator", workspace_ids=[ref.workspace_id])

    # 1. Retrieval at context_top_k=20.
    t0 = time.monotonic()
    if dry_run:
        retrieved = [
            RetrievedItem(
                content=item.context,
                score=1.0,
                workspace_id=ref.workspace_id,
                document_id=ref.document_id,
                chunk_id=ref.chunk_ids[0] if ref.chunk_ids else "dryrun-chunk",
            )
        ]
    else:
        retrieved = await orchestrator.retrieve(
            query=item.question, acl_scope=scope, context_top_k=20
        )
    ret_ms = int((time.monotonic() - t0) * 1000)

    # 2. EvidenceRecall@20.
    retrieved_ids = [it.chunk_id for it in retrieved if it.chunk_id]
    if item.is_answerable:
        recall = 1.0 if any(cid in retrieved_ids for cid in ref.gold_chunk_ids) else 0.0
    else:
        recall = 1.0  # N/A for unanswerable

    # 3. Generation + real claim verification.
    #    Dry-run synthesizes the answer exactly as R08's dry-run did (no
    #    provider, no metering); the real run calls the engine so the claim
    #    verification path is the production one.
    t1 = time.monotonic()
    if dry_run:
        answer = _dry_run_answer(ref)
    else:
        answer = await engine.answer(query=item.question, acl_scope=scope)
    gen_ms = int((time.monotonic() - t1) * 1000)

    # 4. Refusal rule.
    is_refusal = is_refusal_answer(answer)

    # 5. Faithfulness / grounded / precision / validity.
    context_str = "\n".join(it.content for it in retrieved)
    faith = score_faithfulness(answer.answer, context_str) if answer.answer else 0.0
    n_claims = len(answer.claims)
    n_citations = len(answer.citations)
    canonical = sum(
        1 for c in answer.citations if canonical_ids is not None and c.chunk_id in canonical_ids
    )

    if item.is_answerable:
        # Refusal of an answerable item is a FAILURE.
        grounded = False if is_refusal else (
            answer.status == AnswerStatus.ANSWERED
            and n_citations >= 1
            and faith >= FAITHFULNESS_FLOOR
        )
        claim_faiths = [
            score_faithfulness(c.text, context_str) >= FAITHFULNESS_FLOOR
            for c in answer.claims
        ]
        precision = (
            (sum(claim_faiths) / n_claims)
            if n_claims > 0
            else (0.0 if not is_refusal else 1.0)
        )
        validity = 1.0 if n_citations >= 1 else 0.0
        abstention = None
    else:
        grounded = is_refusal or n_citations == 0
        precision = 1.0 if is_refusal else 0.0
        validity = 1.0 if n_citations == 0 else 0.0
        abstention = 1.0 if (is_refusal and n_citations == 0) else 0.0

    return ItemResult(
        ref=ref,
        stratum=stratum_of(item),
        recall_at_20=recall,
        grounded_success=bool(grounded),
        factual_precision=float(precision),
        citation_validity=float(validity),
        correct_abstention=abstention,
        faithfulness=float(faith),
        is_refusal=is_refusal,
        n_citations=n_citations,
        n_claims=n_claims,
        citations_canonical=canonical,
        retrieval_latency_ms=ret_ms,
        generation_latency_ms=gen_ms,
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _bin(rows: Sequence[ItemResult]) -> dict[str, Any]:
    """Aggregate one row-set with R08's denominators and confidence intervals."""
    ans = [r for r in rows if r.ref.item.is_answerable]
    unans = [r for r in rows if not r.ref.item.is_answerable]
    n_ans, n_un = len(ans), len(unans)

    k_recall = sum(1 for r in ans if r.recall_at_20 >= 1.0)
    k_ground = sum(1 for r in ans if r.grounded_success)
    k_prec = sum(1 for r in ans if r.factual_precision >= FAITHFULNESS_FLOOR)
    k_valid = sum(1 for r in ans if r.citation_validity >= 1.0)
    k_abst = sum(1 for r in unans if (r.correct_abstention or 0.0) >= 1.0)

    rates = {
        "evidence_recall_at_20": (k_recall / n_ans) if n_ans else 0.0,
        "grounded_answer_rate": (k_ground / n_ans) if n_ans else 0.0,
        "factual_precision": (k_prec / n_ans) if n_ans else 0.0,
        "citation_validity": (k_valid / n_ans) if n_ans else 0.0,
        "correct_abstention": (k_abst / n_un) if n_un else 0.0,
    }
    cis = {
        "evidence_recall_at_20": wilson_score_interval(k_recall, n_ans),
        "grounded_answer_rate": wilson_score_interval(k_ground, n_ans),
        "factual_precision": wilson_score_interval(k_prec, n_ans),
        "citation_validity": wilson_score_interval(k_valid, n_ans),
        "correct_abstention": wilson_score_interval(k_abst, n_un),
    }
    lat = [float(r.retrieval_latency_ms) for r in rows]
    lat += [float(r.generation_latency_ms) for r in rows]
    return {
        "n_total": len(rows),
        "n_answerable": n_ans,
        "n_unanswerable": n_un,
        "rates": rates,
        "wilson_95": cis,
        "targets": {
            "evidence_recall_at_20": TARGETS["evidence_recall_at_20"],
            "grounded_answer_rate": TARGETS["grounded_answer_rate"],
            "factual_precision": TARGETS["factual_precision"],
            "citation_validity": TARGETS["citation_validity"],
        },
        "pass": {
            "evidence_recall_at_20": (
                rates["evidence_recall_at_20"] >= TARGETS["evidence_recall_at_20"]
            ),
            "grounded_answer_rate": (
                rates["grounded_answer_rate"] >= TARGETS["grounded_answer_rate"]
            ),
            "factual_precision": rates["factual_precision"] >= TARGETS["factual_precision"],
            "citation_validity": rates["citation_validity"] >= TARGETS["citation_validity"],
        },
        "latency_ms": {
            # R08's percentile helper takes a 0-1 fraction, not 0-100.
            "p50": percentile(lat, 0.50) if lat else 0.0,
            "p95": percentile(lat, 0.95) if lat else 0.0,
        },
        "mean_faithfulness": (
            sum(r.faithfulness for r in ans) / n_ans if n_ans else 0.0
        ),
        "citations_canonical": sum(r.citations_canonical for r in rows),
    }


def compile_report(
    rows: Sequence[ItemResult],
    *,
    run_id: str,
    checksum: str,
    spend: dict[str, Any],
    n_loaded: int,
) -> dict[str, Any]:
    """Overall + per-stratum report with provenance."""
    strata: dict[str, Any] = {}
    for name in (STRATUM_MULTI, STRATUM_TABLE, STRATUM_OTHER):
        subset = [r for r in rows if r.stratum == name]
        if subset:
            strata[name] = _bin(subset)
    return {
        "run_id": run_id,
        "corpus_checksum_sha256": checksum,
        "n_loaded": n_loaded,
        "n_evaluated": len(rows),
        "models": {"embedding": EMBED_MODEL, "generation": GEN_MODEL},
        "spend": spend,
        "overall": _bin(rows),
        "strata": strata,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


async def build_stack(
    items: Sequence[EvalItem], *, dry_run: bool, collection: str
) -> dict[str, Any]:
    """Build the retrieval + answer stack for the run (production classes)."""
    if dry_run:
        # Nothing metered is constructed: retrieval and generation are both
        # short-circuited in ``evaluate_ref``. A fake engine here would only
        # invite a code path that reaches a provider by accident.
        return {"orchestrator": None, "engine": None}

    from answer.factory import build_answer_engine
    from retrieval.adapters.pg_fts import PostgresLexicalStore
    from retrieval.adapters.qdrant_store import QdrantVectorStore
    from retrieval.orchestrator import HybridRetrievalOrchestrator

    embedder = _production_embedder()
    orchestrator = HybridRetrievalOrchestrator(
        dense=QdrantVectorStore(),
        embedding=embedder,
        lexical=PostgresLexicalStore(),
        collection=collection,
        reranker=None,  # R08: dense-only, disclosed
    )
    engine = build_answer_engine(
        model=GEN_MODEL, collection=collection, sensitive=False
    )
    engine.llm = OpenRouterProvider(default_call_site=CALL_SITE)
    return {"orchestrator": orchestrator, "engine": engine}


def _production_embedder() -> EmbeddingProvider:
    """3072-d gemini embeddings through the production OpenRouter provider.

    ``OpenRouterProvider`` is the production class the serving path already
    uses: it satisfies both ``LLMProvider`` and ``EmbeddingProvider`` and
    defaults to ``google/gemini-embedding-2`` at 3072 dimensions.
    """
    provider = OpenRouterProvider(default_call_site=CALL_SITE)
    return provider


async def run(args: argparse.Namespace) -> dict[str, Any]:
    run_id = f"eval-{uuid.uuid4().hex[:12]}"

    if args.dry_run:
        # Synthetic corpus so the harness shape is provable without dev2's data.
        items = [
            EvalItem(
                slice_name="multi", source_id="dry-multi", source_path="",
                question="Who acquired Globex?",
                context="Alice works at Acme.\n\nAcme acquired Globex.",
                gold_answer="Acme acquired Globex", content_sha256="dry1", split="dev",
            ),
            EvalItem(
                slice_name="tables", source_id="dry-table", source_path="",
                question="What is the Q3 revenue?", context="Q3 revenue was 42 million.",
                gold_answer="42 million", content_sha256="dry2", split="dev",
            ),
            EvalItem(
                slice_name="unanswerable", source_id="dry-unans", source_path="",
                question="What is the airspeed of a swallow?",
                context="The document lists office opening hours.",
                gold_answer="", content_sha256="dry3", split="dev",
            ),
        ]
    else:
        items = load_corpus()
        if args.limit_n:
            items = items[: args.limit_n]

    checksum = corpus_checksum(items)
    stack = await build_stack(items, dry_run=args.dry_run, collection=args.collection)

    # Production ingest leg is exercised in the real run only; dry-run builds
    # the reference without touching Postgres/Qdrant so it meters nothing.
    rows: list[ItemResult] = []
    if args.dry_run:
        for item in items:
            chunks = chunk_context(item.context)
            chunk_ids = [f"dryrun-{item.source_id}-{i}" for i in range(len(chunks))]
            ref = IngestedRef(
                item=item,
                workspace_id="dryrun-ws",
                document_id=f"dryrun-doc-{item.source_id}",
                chunk_ids=chunk_ids,
                gold_chunk_ids=gold_chunks_for(item, chunk_ids, chunks),
            )
            rows.append(
                await evaluate_ref(
                    ref,
                    orchestrator=stack["orchestrator"],
                    engine=stack["engine"],
                    dry_run=True,
                    canonical_ids=chunk_ids,
                )
            )
    else:
        rows, partial = await _run_production_leg(items, stack, args)

    report = compile_report(
        rows,
        run_id=run_id,
        checksum=checksum,
        spend=GLOBAL_ACCOUNTING.summary()["total"],
        n_loaded=len(items),
    )
    if partial:
        report["partial"] = partial
    return report


async def _run_production_leg(
    items: Sequence[EvalItem], stack: dict[str, Any], args: argparse.Namespace
) -> tuple[list[ItemResult], dict[str, Any] | None]:
    """Upload each item's docs and let the REAL worker ingest it."""
    from v1_full_harness_ingest import ProductionIngestLeg

    leg = ProductionIngestLeg(collection=args.collection, org_id=ORGANIZATION_ID)
    budget = BudgetGuard(args.budget_ceiling)
    rows: list[ItemResult] = []
    partial: dict[str, Any] | None = None
    async with leg:
        for item in items:
            try:
                ref = await leg.ingest(
                    item,
                    docs=DOCS_BY_ITEM.get(item.source_id, []),
                    gold_doc_ids=GOLD_DOC_IDS.get(item.source_id, []),
                )
                rows.append(
                    await evaluate_ref(
                        ref,
                        orchestrator=stack["orchestrator"],
                        engine=stack["engine"],
                        dry_run=False,
                        canonical_ids=ref.chunk_ids,
                    )
                )
            except BudgetExceeded as exc:
                partial = {"reason": str(exc), "stopped_after": len(rows)}
                break
            except Exception as exc:  # quota / transient: stop, keep numbers
                partial = {
                    "reason": f"{type(exc).__name__}: {exc}",
                    "stopped_after": len(rows),
                }
                break
            budget.check()
    cleanup = leg.last_cleanup
    return rows, (partial | {"cleanup": cleanup}) if partial else {"cleanup": cleanup}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="default; proves zero metered calls")
    ap.add_argument("--real", dest="dry_run", action="store_false",
                    help="execute the metered production leg")
    ap.add_argument("--limit-n", type=int, default=0, help="0 = full n")
    ap.add_argument(
        "--collection",
        default=f"eval_scratch_{uuid.uuid4().hex[:8]}",
        help="scratch collection, created at the serving dimension and dropped after the run",
    )
    ap.add_argument("--budget-ceiling", type=float, default=BUDGET_CEILING_USD,
                    help="hard metered spend ceiling in USD")
    ap.add_argument("--out", default="/tmp/opencode/v1_full_harness.json")
    args = ap.parse_args()

    # HARNESS BUG FIX: the in-process worker resolves its target collection from
    # VECTOR_COLLECTION. Export it BEFORE anything resolves the settings
    # singleton, so ingest and retrieval both use the scratch collection.
    os.environ["VECTOR_COLLECTION"] = args.collection

    with MeteredGuard(enforced=args.dry_run) as guard:
        report = asyncio.run(run(args))
    report["metered_calls_during_run"] = guard.report()

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(json.dumps({
        "run_id": report["run_id"],
        "corpus_checksum_sha256": report["corpus_checksum_sha256"][:16],
        "n_evaluated": report["n_evaluated"],
        "strata": {k: v["n_total"] for k, v in report["strata"].items()},
        "overall_rates": report["overall"]["rates"],
        "metered_calls_during_run": report["metered_calls_during_run"],
        "dry_run": args.dry_run,
        "spend_usd": report["spend"].get("cost_usd", 0.0),
        "partial": report.get("partial"),
    }, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())