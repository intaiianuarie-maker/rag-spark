"""Evaluation harness models (TASK-M8-001) — dataset/experiment/run.

Dataset shape mirrors the golden seed in TESTING.md
(``evals/golden.jsonl``): each example carries a question, its expected answer,
and the expected source document ids (ground truth for retrieval metrics),
optionally tagged (EN/RO/PT, table, multi-doc). Runs snapshot the config used
so results are reproducible and comparable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class GoldenExample(BaseModel):
    """One golden Q/A row: the unit the harness scores."""

    question: str
    expected_answer: str = ""
    expected_sources: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class EvaluationDataset(BaseModel):
    id: str
    workspace_id: str | None = None
    name: str
    examples: list[GoldenExample] = Field(default_factory=list)


class EvaluationRun(BaseModel):
    id: str
    dataset_id: str
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    #: metric name -> mean over the dataset (0.0 when the dataset is empty).
    results: dict[str, float] = Field(default_factory=dict)
    #: Total cost (USD) accumulated across questions.
    cost: float = 0.0
    #: Total latency in milliseconds accumulated across questions.
    latency_ms: float = 0.0
    #: Total tokens consumed across questions.
    tokens: int = 0
    #: False when an upstream retrieval adapter did not report its usage.
    accounting_complete: bool = False
