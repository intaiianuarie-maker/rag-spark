"""G5 eval wiring - EvidenceRecall@20 + groundedness, no gate thresholds.

Reuses app.eval.metrics primitives, wires what was unwired.
Uses scratch collection only, proves live stability is separate.
"""
import re

from app.eval.metrics import compute_recall, score_faithfulness


def normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s.strip().lower())

def map_gold_to_chunks(gold_sentence: str, chunks: list[dict]) -> list[str]:
    """Span-based mapping: gold sentence -> chunk_ids containing it."""
    norm_gold = normalize(gold_sentence)
    found = []
    for ch in chunks:
        if norm_gold in normalize(ch["text"]):
            found.append(ch["id"])
    return found

def evidence_recall_at_k(gold_chunk_ids: set[str], retrieved_ids: list[str], k: int = 20) -> float:
    """Wired via compute_recall on truncated ranking."""
    return compute_recall(retrieved_ids[:k], list(gold_chunk_ids))

def groundedness_score(answer: str, context: str) -> float:
    return score_faithfulness(answer, context)
