"""T-V1-076 — the three missing F1 evaluation harnesses.

These close the gap my T-V1-036 readiness probe found: `apps/api/app/eval/`
already had the metric *primitives* but no runnable harness wired any of them,
so RP-02's factual / Recall@20 / abstention targets were unmeasurable.

Design rules honoured here:

* **Reuse, never reimplement.** ``claim_support_rate`` is built on
  :func:`app.eval.metrics.score_faithfulness`; ``recall_at_k`` is a truncation
  of the retrieved list handed to :func:`app.eval.metrics.compute_recall`. No
  metric is re-derived from scratch.
* **Oracle and actual-output metrics stay separated.** Every function that
  reports a number says in its return value whether it is ``oracle=True``. The
  T-V1-036 reviewer checklist requires oracle metrics to be clearly separated
  from actual-output metrics, and a single number that silently mixed them is
  how a gold-context number gets cited as a live-retrieval number.
* **No threshold is invented to be passed.** Cutoffs are named constants with
  the card target they come from, and the rate is reported whatever it is.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.eval.metrics import compute_recall, score_faithfulness

#: Per-claim support cutoff for the *factual* harness. A claim counts as
#: supported when its faithfulness against the gold evidence reaches this. This
#: is the per-claim criterion; the >=98% figure in the T-V1-036 card is the
#: *rate* target and is asserted by the caller, not baked in here.
FACTUAL_SUPPORT_CUTOFF = 0.90

#: RP-02 target for retrieval recall.
RECALL_AT_K = 20
RECALL_TARGET = 0.95

#: RP-02 target for abstention on unanswerable material.
ABSTENTION_TARGET = 0.95

#: Interrogative + function words stripped when a naive system restates an
#: unanswerable question as a factual claim. Keeping the content words is
#: deliberately the HARDER construction: it maximises lexical overlap with the
#: context, so if the product still fails to abstain here, it is genuinely
#: over-supporting — not passing because the probe was too easy to reject.
_STOPWORDS = frozenset(
    """
    a an the of in on at to for from by with and or is are was were be been being
    what which who whom whose when where why how did does do done that this these those
    many much more most number reported report total specific during between within
    """.split()
)


def naive_restatement(question: str) -> str:
    """Deterministically restate a question as a claim a system might assert.

    No LLM and no randomness: the same question always yields the same string,
    which is what makes the abstention gate reproducible.
    """
    words = re.findall(r"[\w'$%.-]+", question)
    kept = [w for w in words if w.lower() not in _STOPWORDS and len(w) > 1]
    return " ".join(kept) if kept else question.strip()


@dataclass
class FactualReport:
    """% of claims supported by their gold evidence."""

    n_claims: int
    supported: int
    rate: float
    oracle: bool = True
    unsupported_ids: list[str] = field(default_factory=list)


def claim_support_rate(
    pairs: Iterable[tuple[str, str, str]],
    *,
    cutoff: float = FACTUAL_SUPPORT_CUTOFF,
) -> FactualReport:
    """Fraction of ``(claim_id, claim, gold_evidence)`` triples that are supported.

    Reuses :func:`score_faithfulness`. ``oracle=True``: the evidence handed in
    is the gold evidence, so this measures the claim layer against gold, never
    against what retrieval actually returned.
    """
    supported = 0
    ids: list[str] = []
    total = 0
    for claim_id, claim, evidence in pairs:
        total += 1
        if score_faithfulness(claim, evidence) >= cutoff:
            supported += 1
        else:
            ids.append(claim_id)
    rate = (supported / total) if total else 0.0
    return FactualReport(n_claims=total, supported=supported, rate=rate, unsupported_ids=ids)


def recall_at_k(
    gold_ids: Sequence[str], retrieved_ids: Sequence[str], k: int = RECALL_AT_K
) -> float:
    """Recall@k over ranked retrieved ids.

    A truncation wrapper around :func:`compute_recall`, not a reimplementation:
    recall@k IS recall computed over the top-k of the ranking. The caller passes
    results in rank order and this slices before delegating.
    """
    return compute_recall(list(retrieved_ids)[:k], list(gold_ids))


@dataclass
class AbstentionReport:
    """% of unanswerable items the product correctly refused to answer."""

    n_items: int
    abstained: int
    rate: float
    oracle: bool = True
    leaked: list[str] = field(default_factory=list)


def evaluate_abstention(
    pairs: Sequence[tuple[str, list[Any], str]],
    *,
    self_check: bool = True,
) -> AbstentionReport:
    """Measure abstention over ``(id, retrieved_items, asserted_claim)`` triples.

    Each triple asks the *real* product verification path whether the asserted
    claim survives. The correct outcome on unanswerable material is that nothing
    survives and the engine declines to answer — that is the abstention this
    counts.

    What this measures, stated precisely so it is not over-read: it exercises
    ``answer.verification.verify_claims`` with the real ``ExtractiveGrounder``,
    i.e. **the claim gate's refusal behaviour**. It does **not** measure whether
    a generator chooses to emit a claim in the first place — the claim is
    supplied by the probe as the naive restatement of the question. Measuring
    generator-side abstention needs an LLM in the loop and is deliberately out of
    this hermetic harness's scope (it would break the Q9 bench's reproducibility).
    """
    from answer.models import Claim
    from answer.verification import ExtractiveGrounder

    grounder = ExtractiveGrounder()

    if self_check and pairs:
        _abstention_self_check(grounder, pairs[0][1])

    abstained = 0
    leaked: list[str] = []
    for item_id, items, claim in pairs:
        claims = [
            Claim(text=claim, chunk_ids=[i.chunk_id for i in items if i.chunk_id])
        ]
        verified, citations = asyncio.run(
            verify_claims_safe(claims, items, grounder)
        )
        if not verified and not citations:
            abstained += 1
        else:
            leaked.append(item_id)
    n = len(pairs)
    return AbstentionReport(
        n_items=n,
        abstained=abstained,
        rate=(abstained / n) if n else 0.0,
        leaked=leaked,
    )


def _first_sentence(text: str) -> str:
    """First non-empty sentence of ``text`` (decimal-safe on digit.period)."""
    for match in re.finditer(r"[^.!?]*[.!?](?:\s|$)", text.strip()):
        candidate = match.group(0).strip()
        if candidate:
            return candidate
    return text.strip()


def _abstention_self_check(grounder: Any, items: Sequence[Any]) -> None:
    """Prove the harness can certify a SUPPORTED claim before trusting a 1.0.

    An abstention rate of 1.0 is only meaningful if the same verification path
    would have certified a genuinely supported claim. Without this, a grounder
    that rejects *everything* (misconfigured, or a strict-fragment policy
    applied to whole sentences) would report a perfect abstention score while
    measuring nothing at all.

    The control asserts a whole sentence copied verbatim from the first chunk:
    ``ExtractiveGrounder`` requires verbatim, word-bounded containment inside a
    sentence that is itself a whole-sentence assertion, so this is the shape it
    is contractually obliged to accept.
    """
    if not items:
        raise RuntimeError("abstention harness self-check needs at least one retrieved item")
    from answer.models import Claim

    # Try the sentences of each item until one verifies. A chunk that is a
    # SINGLE sentence does not self-verify under this grounder (empirically: a
    # one-sentence chunk scores 0.0 even against a claim copied from it), so a
    # control must draw on a multi-sentence chunk.
    for item in items:
        for sentence in _sentences(item.content):
            if not sentence:
                continue
            verified, citations = asyncio.run(
                verify_claims_safe(
                    [Claim(text=sentence, chunk_ids=[item.chunk_id])], [item], grounder
                )
            )
            if verified and citations:
                return
    raise RuntimeError(
        "abstention harness self-check FAILED: no verbatim sentence from the "
        "retrieved context survived verification, so an abstention rate of 1.0 "
        "would be vacuous. Do not report an abstention number from this run."
    )


def _sentences(text: str) -> list[str]:
    """All non-empty sentences of ``text`` (decimal-safe on digit.period)."""
    out = [m.group(0).strip() for m in re.finditer(r"[^.!?]*[.!?](?:\s|$)", text.strip())]
    return [s for s in out if s] or ([text.strip()] if text.strip() else [])


async def verify_claims_safe(
    claims: list[Any], items: list[Any], grounder: Any
) -> tuple[list[Any], list[Any]]:
    """Thin pass-through to the real verification path (import kept lazy)."""
    from answer.verification import verify_claims

    return await verify_claims(
        claims,
        items,
        allowed_workspace_ids={i.workspace_id for i in items},
        claim_grounder=grounder,
    )