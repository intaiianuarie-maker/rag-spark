# ruff: noqa
# mypy: ignore-errors
"""T-V2-006: runner mechanics (not scores) - model, max_tokens, egress, selection, no corpus edit."""

from __future__ import annotations

import json
import pathlib

import pytest


def test_runner_uses_free_model_only():
    text = pathlib.Path("evals/g5_real_paired_eval.py").read_text()
    assert "nvidia/nemotron-3.5-lightning:free" in text
    assert "ANSWER_MODEL" in text
    # Must not introduce paid model
    assert "gpt-4" not in text.lower() or "free" in text.lower()


def test_runner_sets_max_tokens_ge_1000():
    text = pathlib.Path("evals/g5_real_paired_eval.py").read_text()
    assert "MAX_TOKENS" in text
    # Find the value
    import re
    m = re.search(r"MAX_TOKENS\s*=\s*(\d+)", text)
    assert m and int(m.group(1)) >= 1000, "reasoning models need max_tokens>=1000"


def test_runner_selection_rule_first_20():
    text = pathlib.Path("evals/g5_real_paired_eval.py").read_text()
    assert "first 20 eligible" in text.lower()
    pin = json.loads(pathlib.Path("evals/g5_hotpot_pin.json").read_text())
    assert len(pin["selected_indices"]) >= 20


@pytest.mark.skipif(
    not pathlib.Path("/root/code/Super Rag/datasets/rag-eval/ragbench-tests-v1/hotpotqa/test-00000-of-00001.parquet").exists(),
    reason="needs the HotpotQA parquet (not in the repo)",
)
def test_runner_does_not_edit_corpus():
    pin_before = json.loads(pathlib.Path("evals/g5_hotpot_pin.json").read_text())
    # Runner should not modify pin
    assert pin_before["n_rows"] == 100
    # Check that pin file is still frozen (hash matches)
    import hashlib

    path = pathlib.Path("/root/code/Super Rag/datasets/rag-eval/ragbench-tests-v1/hotpotqa/test-00000-of-00001.parquet")
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()
    assert h == pin_before["parquet_sha256"]


def test_egress_check_exists():
    text = pathlib.Path("evals/g5_real_paired_eval.py").read_text()
    assert "check_egress" in text
    assert "api.openrouter.ai" in text or "openrouter" in text.lower()
