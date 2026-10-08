"""Grounded-answer prompt construction (TASK-M4-002).

The prompt is deliberately strict: answer only from the supplied context, and
emit a JSON object with ``answer`` and ``claims`` (each claim tied to the
``chunk_id``s it is grounded in). The model is never asked to invent citations —
the API verifies and strips any chunk id that is missing or out of scope.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from retrieval.interfaces import RetrievedItem

SYSTEM_PROMPT = (
    "You are the knowledge assistant for a document workspace. Answer using "
    "ONLY the provided context. Do not use outside knowledge. If the context "
    "does not contain the answer, say so.\n\n"
    "Respond with a single JSON object of the form:\n"
    '{"answer": "<the answer>", "claims": [{"text": "<a grounded statement>", '
    '"chunk_ids": ["<chunk_id>", ...]}]}\n'
    "Every chunk_id MUST be copied verbatim from the context's chunk_id field. "
    "Never invent a chunk_id."
)


def _context_block(item: RetrievedItem) -> str:
    where = ", ".join(
        part
        for part in (
            f"page {item.page}" if item.page is not None else "",
            item.section or "",
        )
        if part
    )
    location = f" ({where})" if where else ""
    return f"[chunk_id: {item.chunk_id}{location}]\n{item.content}"


def build_prompt(query: str, items: Sequence[RetrievedItem]) -> str:
    """Render the user prompt: the retrieved context followed by the question."""
    context = "\n\n".join(_context_block(item) for item in items)
    return f"Context:\n{context}\n\nQuestion: {query}"


def parse_model_output(text: str) -> dict:
    """Parse the model's JSON object, tolerating code fences and trailing text.

    Returns ``{"answer": str, "claims": [...]}``. Raises :class:`ValueError`
    when the payload is not the expected shape — the engine then falls back to
    the raw text with no claims rather than fabricating structure.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop a single fenced block (```json ... ```).
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(stripped)
    except ValueError:
        # Last resort: find the first {...} block.
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("model output contained no JSON object") from None
        payload = json.loads(stripped[start : end + 1])

    if not isinstance(payload, dict):
        raise ValueError("model output was not a JSON object")
    answer = payload.get("answer")
    if not isinstance(answer, str):
        raise ValueError("model output had no string 'answer'")
    raw_claims = payload.get("claims", [])
    if not isinstance(raw_claims, list):
        raise ValueError("model output 'claims' was not a list")

    claims: list[dict] = []
    for entry in raw_claims:
        if not isinstance(entry, dict):
            continue
        claim_text = entry.get("text")
        if not isinstance(claim_text, str) or not claim_text.strip():
            continue
        chunk_ids = entry.get("chunk_ids", [])
        if not isinstance(chunk_ids, list):
            chunk_ids = []
        claims.append(
            {
                "text": claim_text,
                "chunk_ids": [str(cid) for cid in chunk_ids],
            }
        )
    return {"answer": answer, "claims": claims}
