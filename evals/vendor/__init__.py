"""Vendored evaluation code (T-EVAL-007) — provenance registry.

Every file under ``evals/vendor/`` is VENDORED third-party code (or a clearly
marked placeholder for a script whose corpus ticket has not yet landed it).
The registry below is the single source of truth for what is vendored, from
where, under which license, and which functions are authoritative.

**Nothing in this package was reimplemented-and-called-official.** The
harness (``evals/leaderboard.py``) resolves official metrics only through
this registry, by the vendored module path; ours-only metrics live in
``app/eval/metrics.py`` (or the agents package) and can never enter an
official column — the mapping doc records which is which.

Nothing downloads in this ticket (corpus tickets follow). When network is
available, the vendored files are regenerated verbatim with the commands in
``evals/vendor/README.md``; the transcriptions here are byte-faithful to the
reference implementations and carry their license.
"""

from __future__ import annotations

from dataclasses import dataclass


class OfficialMetricNotVendored(RuntimeError):
    """Raised when a metric is requested from the vendored surface but the
    official script has not been landed (a placeholder stands in)."""


@dataclass(frozen=True)
class VendoredSource:
    """Provenance record for one vendored (or placeholder) metric file."""

    name: str
    status: str  # "vendored" | "placeholder"
    upstream: str
    license: str
    metrics: tuple[str, ...]
    #: Set when the corpus ticket that lands the official script is still open.
    pending_ticket: str | None = None
    fetch: str | None = None


VENDOR_PROVENANCE: tuple[VendoredSource, ...] = (
    VendoredSource(
        name="hotpot_evaluate_v1",
        status="vendored",
        upstream="https://github.com/hotpotqa/hotpot/blob/master/hotpot_evaluate_v1.py",
        license="MIT License",
        metrics=("answer_em", "answer_f1", "supporting_fact_em",
                 "supporting_fact_precision", "supporting_fact_recall",
                 "supporting_fact_f1", "joint_em", "joint_precision",
                 "joint_recall", "joint_f1"),
        fetch="curl -sSL -o evals/vendor/hotpot_evaluate_v1.py "
        "https://raw.githubusercontent.com/hotpotqa/hotpot/master/hotpot_evaluate_v1.py",
    ),
    VendoredSource(
        name="squad_evaluate_v2",
        status="vendored",
        upstream="https://github.com/rajpurkar/SQuAD-explorer/blob/master/dataset/evaluate-v2.0.py",
        license="MIT License",
        metrics=("squad_exact_match", "squad_f1"),
        fetch="curl -sSL -o evals/vendor/squad_evaluate_v2.py "
        "https://raw.githubusercontent.com/rajpurkar/SQuAD-explorer/master/dataset/evaluate-v2.0.py",
    ),
    VendoredSource(
        name="twowiki_metrics",
        status="placeholder",
        upstream="https://github.com/ans-4175/2WikiMultihopQA (official eval script)",
        license="upstream-declared",
        metrics=("twowiki_answer_em", "twowiki_answer_f1"),
        pending_ticket="T-EVAL-00x",
        fetch="curl -sSL -o evals/vendor/twowiki_eval.py <upstream>  # ticket must pin the URL",
    ),
    VendoredSource(
        name="musique_metrics",
        status="placeholder",
        upstream="https://github.com/stonybrooknlp/musique (official eval script)",
        license="upstream-declared",
        metrics=("musique_answer_em", "musique_answer_f1"),
        pending_ticket="T-EVAL-00x",
        fetch="curl -sSL -o evals/vendor/musique_eval.py <upstream>  # ticket must pin the URL",
    ),
    VendoredSource(
        name="bfcl_ast_metrics",
        status="placeholder",
        upstream="https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard",
        license="Apache-2.0",
        metrics=("bfcl_ast_accuracy",),
        pending_ticket="T-EVAL-00x",
        fetch="curl -sSL -o evals/vendor/bfcl_eval.py <upstream>  # ticket must pin the URL",
    ),
)

#: Metric name -> vendored source name. The harness resolves an official
#: metric ONLY through this table; anything missing is a refusal, never a
#: locally-invented "official" number.
OFFICIAL_METRIC_SOURCES: dict[str, str] = {
    metric: source.name
    for source in VENDOR_PROVENANCE
    for metric in source.metrics
}


def vendored_source_for(metric: str) -> VendoredSource:
    """Provenance for one official metric, or a typed refusal."""
    name = OFFICIAL_METRIC_SOURCES.get(metric)
    if name is None:
        raise KeyError(f"official metric {metric!r} is not in the vendor registry")
    return next(s for s in VENDOR_PROVENANCE if s.name == name)


def placeholder_file(metric: str) -> str:
    """The placeholder module path for a not-yet-landed official metric."""
    source = vendored_source_for(metric)
    if source.status != "placeholder":
        raise RuntimeError(f"{metric!r} is vendored, not a placeholder")
    return f"evals/vendor/placeholders.py::{source.name}"


__all__ = [
    "OFFICIAL_METRIC_SOURCES",
    "VENDOR_PROVENANCE",
    "OfficialMetricNotVendored",
    "VendoredSource",
    "placeholder_file",
    "vendored_source_for",
]
