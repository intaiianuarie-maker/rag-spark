"""Leaderboard-comparable results harness (T-EVAL-007).

Two surfaces, structurally separated:

* **OFFICIAL** columns — metric values produced by *vendored* upstream code
  (``evals/vendor/*``) or marked ``pending`` when the official script has not
  been landed yet (a placeholder, never a stand-in number).
* **OURS** columns — our own metrics (recall/precision/MRR/NDCG/citation
  accuracy, the lexical groundedness proxy), never blended into an official
  column; the mapping doc records which is which.

The table shaper emits rows in the public-leaderboard shape (metric columns,
model/dataset rows, one-decimal precision on the percentage-style metrics) so
a row produced here sits next to anyone's published row without conversion.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from evals.vendor import (
    OFFICIAL_METRIC_SOURCES,
    VENDOR_PROVENANCE,
    OfficialMetricNotVendored,
    VendoredSource,
    placeholder_file,
    vendored_source_for,
)
from evals.vendor import hotpot_evaluate_v1 as _hotpot
from evals.vendor import placeholders as _placeholders
from evals.vendor import squad_evaluate_v2 as _squad

#: Public-leaderboard precision for the headline metrics (HotpotQA / SQuAD
#: style: one decimal on a 0-100 scale).
LEADERBOARD_PRECISION = 1

OFFICIAL = "official"
OURS_ONLY = "ours_only"
PENDING = "pending"


class MetricKind:
    OFFICIAL = OFFICIAL
    OURS_ONLY = OURS_ONLY


@dataclass(frozen=True)
class MetricSpec:
    """One metric column: its name, its side and how it is computed."""

    name: str
    kind: str  # OFFICIAL | OURS_ONLY
    #: Callable for a vendored official metric (``None`` for ours).
    official_fn: Callable[..., object] | None = None
    #: Provenance label for ours metrics (where OUR implementation lives).
    ours_source: str = ""
    description: str = ""


@dataclass(frozen=True)
class Cell:
    """One table cell: a value, ``pending`` for a not-yet-landed official
    metric, and the metric kind the cell belongs to."""

    value: float | None
    kind: str
    precision: int = LEADERBOARD_PRECISION

    @property
    def is_pending(self) -> bool:
        return self.value is None

    def render(self) -> str:
        if self.is_pending:
            return PENDING
        value = self.value or 0.0
        return f"{round(value, self.precision):.{self.precision}f}"


@dataclass
class LeaderboardRow:
    """One dataset/model row — official and ours cells kept apart by shape."""

    dataset: str
    model: str
    official: dict[str, Cell] = field(default_factory=dict)
    ours: dict[str, Cell] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The two registries (official from the vendor seam, ours from our package)
# --------------------------------------------------------------------------- #

_VENDORED_FNS: dict[str, Callable[..., object]] = {
    "answer_em": _hotpot.answer_em,
    "answer_f1": _hotpot.answer_f1,
    "supporting_fact_em": _hotpot.supporting_fact_em,
    "supporting_fact_precision": _hotpot.supporting_fact_precision,
    "supporting_fact_recall": _hotpot.supporting_fact_recall,
    "supporting_fact_f1": _hotpot.supporting_fact_f1,
    "joint_em": _hotpot.joint_em,
    "joint_precision": _hotpot.joint_precision,
    "joint_recall": _hotpot.joint_recall,
    "joint_f1": _hotpot.joint_f1,
    "squad_exact_match": _squad.squad_exact_match,
    "squad_f1": _squad.squad_f1,
}

_PLACEHOLDER_FNS: dict[str, Callable[..., object]] = {
    "twowiki_answer_em": _placeholders.twowiki_answer_em,
    "twowiki_answer_f1": _placeholders.twowiki_answer_f1,
    "musique_answer_em": _placeholders.musique_answer_em,
    "musique_answer_f1": _placeholders.musique_answer_f1,
    "bfcl_ast_accuracy": _placeholders.bfcl_ast_accuracy,
}

#: Our own metrics — NEVER blended into official columns. Reuses the
#: ``app/eval/metrics.py`` primitives (recall/precision/MRR/NDCG/citation
#: accuracy and the lexical groundedness proxy) rather than re-deriving them.
from app.eval.metrics import (  # noqa: E402
    compute_citation_accuracy,
    compute_mrr,
    compute_ndcg,
    compute_precision,
    compute_recall,
    score_faithfulness,
)

OURS_METRIC_FNS: dict[str, Callable[..., float]] = {
    "retrieval_recall": compute_recall,
    "retrieval_precision": compute_precision,
    "retrieval_mrr": compute_mrr,
    "retrieval_ndcg": compute_ndcg,
    "citation_accuracy": compute_citation_accuracy,
    "groundedness_lexical_proxy": score_faithfulness,
}

OURS_SOURCE = "app/eval/metrics.py"

MAPPING: dict[str, str] = {
    # Official (vendored) metrics.
    "answer_em": OFFICIAL,
    "answer_f1": OFFICIAL,
    "supporting_fact_em": OFFICIAL,
    "supporting_fact_precision": OFFICIAL,
    "supporting_fact_recall": OFFICIAL,
    "supporting_fact_f1": OFFICIAL,
    "joint_em": OFFICIAL,
    "joint_precision": OFFICIAL,
    "joint_recall": OFFICIAL,
    "joint_f1": OFFICIAL,
    "squad_exact_match": OFFICIAL,
    "squad_f1": OFFICIAL,
    "twowiki_answer_em": OFFICIAL,
    "twowiki_answer_f1": OFFICIAL,
    "musique_answer_em": OFFICIAL,
    "musique_answer_f1": OFFICIAL,
    "bfcl_ast_accuracy": OFFICIAL,
    # Ours-only: no official counterpart; never blended into official columns.
    "retrieval_recall": OURS_ONLY,
    "retrieval_precision": OURS_ONLY,
    "retrieval_mrr": OURS_ONLY,
    "retrieval_ndcg": OURS_ONLY,
    "citation_accuracy": OURS_ONLY,
    "groundedness_lexical_proxy": OURS_ONLY,
}

#: Datasets whose official metrics are fully vendored (runnable today).
DATASETS_OFFICIAL_VENDORED = ("hotpotqa", "xquad")
#: Datasets whose official metrics await their corpus ticket (placeholder).
DATASETS_OFFICIAL_PENDING = ("2wiki", "musique", "bfcl")


def metric_spec(name: str) -> MetricSpec:
    """Resolve one metric to its spec (kind + callable)."""
    kind = MAPPING[name]
    if kind == OFFICIAL:
        return MetricSpec(
            name=name,
            kind=OFFICIAL,
            official_fn=_VENDORED_FNS.get(name, _PLACEHOLDER_FNS.get(name)),
            description=f"official metric vendored from {vendored_source_for(name).upstream}",
        )
    return MetricSpec(
        name=name,
        kind=OURS_ONLY,
        official_fn=None,
        ours_source=OURS_SOURCE,
        description="ours-only metric (no official counterpart)",
    )


def official_pending(name: str) -> bool:
    """Whether an official metric's script has not landed yet."""
    return vendored_source_for(name).status == "placeholder"


def placeholder_path(name: str) -> str:
    return placeholder_file(name)


def make_cell(name: str, value: float | None) -> Cell:
    kind = MAPPING[name]
    return Cell(value=value, kind=kind, precision=LEADERBOARD_PRECISION)


def run_official(name: str, *args: object, **kwargs: object) -> Cell:
    """Run one vendored official metric into a cell.

    A placeholder metric yields a ``pending`` cell (never 0.0, never a local
    stand-in); a vendored metric yields its authoritative value.
    """
    if official_pending(name):
        return Cell(value=None, kind=OFFICIAL, precision=LEADERBOARD_PRECISION)
    spec = metric_spec(name)
    fn = spec.official_fn
    if fn is None:  # pragma: no cover - registry invariant
        raise OfficialMetricNotVendored(f"no vendored implementation for {name!r}")
    value = float(fn(*args, **kwargs))  # type: ignore[arg-type]
    return Cell(value=value, kind=OFFICIAL, precision=LEADERBOARD_PRECISION)


def run_ours(name: str, *args: object, **kwargs: object) -> Cell:
    """Run one of our metrics into an ours cell."""
    fn = OURS_METRIC_FNS[name]
    return Cell(value=float(fn(*args, **kwargs)), kind=OURS_ONLY, precision=LEADERBOARD_PRECISION)


def assert_columns_separate(row: LeaderboardRow) -> None:
    """Structural separation: no metric name may appear on both sides."""
    overlap = set(row.official) & set(row.ours)
    if overlap:
        raise AssertionError(f"official/ours column blend: {sorted(overlap)}")
    for cell in row.official.values():
        assert cell.kind == OFFICIAL, "an official column carries an ours cell"
    for cell in row.ours.values():
        assert cell.kind == OURS_ONLY, "an ours column carries an official cell"


def shape_table(
    rows: Sequence[LeaderboardRow],
    official_columns: Sequence[str],
    ours_columns: Sequence[str],
) -> list[list[str]]:
    """Render leaderboard-shaped rows (header + one row per submission)."""
    header = ["dataset", "model", *official_columns, *ours_columns]
    table = [header]
    for row in rows:
        line = [row.dataset, row.model]
        line.extend(row.official[c].render() if c in row.official else PENDING for c in official_columns)  # noqa: E501
        line.extend(row.ours[c].render() if c in row.ours else PENDING for c in ours_columns)
        table.append(line)
    return table


__all__ = [
    "DATASETS_OFFICIAL_PENDING",
    "DATASETS_OFFICIAL_VENDORED",
    "LEADERBOARD_PRECISION",
    "MAPPING",
    "OFFICIAL",
    "OFFICIAL_METRIC_SOURCES",
    "OURS_ONLY",
    "OURS_SOURCE",
    "PENDING",
    "VENDOR_PROVENANCE",
    "Cell",
    "LeaderboardRow",
    "MetricSpec",
    "VendoredSource",
    "assert_columns_separate",
    "make_cell",
    "metric_spec",
    "official_pending",
    "placeholder_path",
    "run_official",
    "run_ours",
    "shape_table",
    "vendored_source_for",
]
