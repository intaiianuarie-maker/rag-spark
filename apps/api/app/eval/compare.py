"""A/B comparison reports (TASK-M8-001b).

Compares two experiment runs on a single dimension (embedder/chunker/
retriever/top-K/reranker/model/prompt) and reports per-metric deltas plus
cost and latency deltas with direction flags.

One dimension per comparison (no multivariate). Pure + testable; no UI.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .models import EvaluationRun
from .report import REPORT_METRICS, RunSummary, summarize_run

#: Metrics where a larger value is better.
HIGHER_IS_BETTER_METRICS = set(REPORT_METRICS)
#: Metrics where a smaller value is better (cost/latency).
LOWER_IS_BETTER = {"cost", "latency_ms", "tokens"}


class RunRecord(BaseModel):
    """Normalized view of a run used for comparison."""

    run_id: str
    n_questions: int = 0
    metrics: dict[str, float] = Field(default_factory=dict)
    cost: float = 0.0
    latency_ms: float = 0.0
    tokens: int = 0
    dimension: str | None = None
    dimension_value: str | None = None


class MetricDelta(BaseModel):
    metric: str
    baseline: float = 0.0
    candidate: float = 0.0
    delta: float = 0.0
    #: "improved" / "regressed" / "unchanged"
    direction: str = "unchanged"
    higher_is_better: bool = True


class ABComparison(BaseModel):
    """A-vs-B comparison result."""

    baseline_run_id: str
    candidate_run_id: str
    dimension: str | None = None
    metric_deltas: list[MetricDelta] = Field(default_factory=list)
    cost_delta: float = 0.0
    latency_delta_ms: float = 0.0
    tokens_delta: int = 0
    cost_direction: str = "unchanged"
    latency_direction: str = "unchanged"
    #: metric -> run id of the better side ("baseline"/"candidate")
    winners: dict[str, str] = Field(default_factory=dict)
    configuration_validated: bool = False


def _validate_pair(baseline: Any, candidate: Any, dimension: str | None) -> bool:
    """Real saved runs must share inputs and vary exactly one declared setting."""
    if not isinstance(baseline, EvaluationRun) or not isinstance(candidate, EvaluationRun):
        return False  # synthetic summary arithmetic has no configuration evidence
    a, b = baseline.config_snapshot, candidate.config_snapshot
    if baseline.dataset_id != candidate.dataset_id:
        raise ValueError("comparison requires the same dataset")
    if not a and not b:
        return False  # backwards-compatible arithmetic on synthetic canned pairs
    for key in ("dataset_sha256", "n_questions", "scorer", "sensitive", "accounting"):
        if key not in a or key not in b or a[key] != b[key]:
            raise ValueError(f"incomparable run snapshots: {key}")
    if not isinstance(a.get("retrieval"), dict) or not isinstance(b.get("retrieval"), dict):
        raise ValueError("comparison requires retrieval configuration snapshots")
    if baseline.accounting_complete != candidate.accounting_complete:
        raise ValueError("incomparable accounting completeness")
    allowed = {"top_k", "mode", "reranker", "embedder", "chunker", "retriever", "model", "prompt"}
    if dimension not in allowed:
        raise ValueError("dimension must identify an actual configurable retrieval setting")
    settings_a = {**a["retrieval"], **a.get("experiment", {})}
    settings_b = {**b["retrieval"], **b.get("experiment", {})}
    changed = {
        key
        for key in set(settings_a) | set(settings_b)
        if settings_a.get(key) != settings_b.get(key)
    }
    if changed != {dimension}:
        raise ValueError("runs must differ only in the selected dimension")
    return True


def _to_record(
    obj: Any, dimension: str | None = None, dimension_value: str | None = None
) -> RunRecord:
    if isinstance(obj, RunRecord):
        rec = obj.model_copy()
        if dimension is not None:
            rec.dimension = dimension
        if dimension_value is not None:
            rec.dimension_value = dimension_value
        return rec
    if isinstance(obj, EvaluationRun):
        return RunRecord(
            run_id=obj.id,
            n_questions=int(obj.config_snapshot.get("n_questions", 0)),
            metrics=dict(obj.results or {}),
            cost=float(obj.cost),
            latency_ms=float(obj.latency_ms),
            tokens=int(obj.tokens),
            dimension=dimension,
            dimension_value=dimension_value,
        )
    if isinstance(obj, RunSummary):
        return RunRecord(
            run_id=obj.run_id,
            n_questions=obj.n_questions,
            metrics=dict(obj.means or {}),
            cost=float(obj.cost),
            latency_ms=float(obj.latency_ms),
            tokens=int(obj.tokens),
            dimension=dimension,
            dimension_value=dimension_value,
        )
    if isinstance(obj, list):
        # list of MetricRow
        if obj and hasattr(obj[0], "run_id"):
            rid = obj[0].run_id
            summary = summarize_run(rid, obj)
            return _to_record(summary, dimension, dimension_value)
        # list of dicts
        metrics: dict[str, float] = {}
        cost = 0.0
        lat = 0.0
        toks = 0
        for item in obj:
            if not isinstance(item, dict):
                continue
            for k, v in item.items():
                if isinstance(v, (int, float)) and k not in ("cost", "latency_ms", "tokens"):
                    metrics[k] = metrics.get(k, 0.0) + float(v)
            cost += float(item.get("cost", 0.0))
            lat += float(item.get("latency_ms", 0.0))
            toks += int(item.get("tokens", 0))
        n = len(obj)
        if n:
            metrics = {k: v / n for k, v in metrics.items()}
        return RunRecord(
            run_id=str(obj[0].get("run_id", "run")) if obj and isinstance(obj[0], dict) else "run",
            n_questions=n,
            metrics=metrics,
            cost=cost,
            latency_ms=lat,
            tokens=toks,
            dimension=dimension,
            dimension_value=dimension_value,
        )
    if isinstance(obj, dict):
        metrics = {}
        for k in REPORT_METRICS:
            if k in obj:
                metrics[k] = float(obj[k])
        # also accept a nested "metrics"/"means"/"results"
        for key in ("metrics", "means", "results"):
            if isinstance(obj.get(key), dict):
                for k, v in obj[key].items():
                    try:
                        metrics[k] = float(v)
                    except (TypeError, ValueError):
                        pass
        return RunRecord(
            run_id=str(obj.get("run_id") or obj.get("id") or "run"),
            n_questions=int(obj.get("n_questions", 0)),
            metrics=metrics,
            cost=float(obj.get("cost", 0.0)),
            latency_ms=float(obj.get("latency_ms", 0.0)),
            tokens=int(obj.get("tokens", 0)),
            dimension=dimension or obj.get("dimension"),
            dimension_value=dimension_value or obj.get("dimension_value"),
        )
    # fallback: try attributes
    run_id = getattr(obj, "run_id", None) or getattr(obj, "id", None) or "run"
    metrics = (
        getattr(obj, "metrics", None)
        or getattr(obj, "means", None)
        or getattr(obj, "results", None)
        or {}
    )
    metrics = {k: float(v) for k, v in dict(metrics).items()} if isinstance(metrics, dict) else {}
    return RunRecord(
        run_id=str(run_id),
        n_questions=int(getattr(obj, "n_questions", 0)),
        metrics=metrics,
        cost=float(getattr(obj, "cost", 0.0)),
        latency_ms=float(getattr(obj, "latency_ms", 0.0)),
        tokens=int(getattr(obj, "tokens", 0)),
        dimension=dimension,
        dimension_value=dimension_value,
    )


def _direction(delta: float, higher_is_better: bool) -> str:
    if delta == 0:
        return "unchanged"
    if higher_is_better:
        return "improved" if delta > 0 else "regressed"
    return "improved" if delta < 0 else "regressed"


def compare_runs(
    baseline: Any,
    candidate: Any,
    *,
    dimension: str | None = None,
    dimension_value_baseline: str | None = None,
    dimension_value_candidate: str | None = None,
) -> ABComparison:
    """Compare candidate vs baseline on a single dimension.

    ``delta = candidate - baseline`` for every metric. Direction flags use
    higher-is-better for quality metrics and lower-is-better for cost/latency/
    tokens.
    """
    validated = _validate_pair(baseline, candidate, dimension)
    a = _to_record(baseline, dimension, dimension_value_baseline)
    b = _to_record(candidate, dimension, dimension_value_candidate)

    all_metrics = sorted(set(a.metrics) | set(b.metrics) | set(REPORT_METRICS))

    metric_deltas: list[MetricDelta] = []
    winners: dict[str, str] = {}
    for m in all_metrics:
        va = float(a.metrics.get(m, 0.0))
        vb = float(b.metrics.get(m, 0.0))
        delta = vb - va
        higher = m in HIGHER_IS_BETTER_METRICS or m not in LOWER_IS_BETTER
        md = MetricDelta(
            metric=m,
            baseline=va,
            candidate=vb,
            delta=delta,
            direction=_direction(delta, higher),
            higher_is_better=higher,
        )
        metric_deltas.append(md)
        if (delta > 0 and higher) or (delta < 0 and not higher):
            winners[m] = b.run_id
        elif delta != 0:
            winners[m] = a.run_id
        else:
            winners[m] = a.run_id

    cost_delta = b.cost - a.cost
    lat_delta = b.latency_ms - a.latency_ms
    tok_delta = b.tokens - a.tokens

    return ABComparison(
        baseline_run_id=a.run_id,
        candidate_run_id=b.run_id,
        dimension=dimension or a.dimension or b.dimension,
        metric_deltas=metric_deltas,
        cost_delta=cost_delta,
        latency_delta_ms=lat_delta,
        tokens_delta=tok_delta,
        cost_direction=_direction(cost_delta, False),
        latency_direction=_direction(lat_delta, False),
        winners=winners,
        configuration_validated=validated,
    )


def build_ab_report(
    baseline: Any,
    candidate: Any,
    *,
    dimension: str | None = None,
    **kwargs: Any,
) -> ABComparison:
    """Alias for :func:`compare_runs` with a report-style name."""
    return compare_runs(baseline, candidate, dimension=dimension, **kwargs)


def render_comparison_text(comparison: ABComparison) -> str:
    """Render a human-readable delta table (Experiment-#N style)."""
    lines = [
        f"A/B comparison: {comparison.baseline_run_id} vs {comparison.candidate_run_id}",
    ]
    if comparison.dimension:
        lines.append(f"dimension: {comparison.dimension}")
    lines.append("")
    lines.append(f"{'metric':<20}{'baseline':>12}{'candidate':>12}{'delta':>12}  direction")
    for md in comparison.metric_deltas:
        lines.append(
            f"{md.metric:<20}{md.baseline:>12.4f}"
            f"{md.candidate:>12.4f}{md.delta:>12.4f}  {md.direction}"
        )
    lines.append("")
    lines.append(f"{'cost':<20}{comparison.cost_delta:>+12.4f}  {comparison.cost_direction}")
    lines.append(
        f"{'latency_ms':<20}{comparison.latency_delta_ms:>+12.2f}  {comparison.latency_direction}"
    )
    lines.append(f"{'tokens':<20}{comparison.tokens_delta:>+12d}")
    return "\n".join(lines)
