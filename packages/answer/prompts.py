"""Grounded-answer prompt construction (TASK-M4-002).

The prompt is deliberately strict: answer only from the supplied context, and
emit a JSON object with ``answer`` and ``claims`` (each claim tied to the
``chunk_id``s it is grounded in). The model is never asked to invent citations —
the API verifies and strips any chunk id that is missing or out of scope.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence

from retrieval.interfaces import RetrievedItem

SYSTEM_PROMPT = (
    "You are the knowledge assistant for a document workspace. Answer using "
    "ONLY the provided context. Do not use outside knowledge. If the context "
    "does not contain the answer, say so.\n\n"
    "Respond with a single JSON object of the form:\n"
    '{"answer": "<the answer>", "claims": [{"text": "<a grounded statement>", '
    '"chunk_ids": ["<chunk_id>", ...]}]}\n'
    "Every chunk_id MUST be copied verbatim from the context's chunk_id field. "
    "Cite ONLY the bare id exactly as written inside the brackets after "
    '"chunk_id:" (a UUID). Never add the page, section, "Location" text, '
    "brackets or any other words to a chunk_id. Never invent a chunk_id."
)


def _context_block(item: RetrievedItem) -> str:
    # The bracket holds the bare id only: models copy the whole bracket text as
    # the citation id, so page/section live on their own line outside it.
    where = ", ".join(
        part
        for part in (
            f"page {item.page}" if item.page is not None else "",
            item.section or "",
        )
        if part
    )
    location = f"\nLocation: {where}" if where else ""
    return f"[chunk_id: {item.chunk_id}]{location}\n{item.content}"


def build_prompt(query: str, items: Sequence[RetrievedItem]) -> str:
    """Render the user prompt: the retrieved context followed by the question."""
    context = "\n\n".join(_context_block(item) for item in items)
    return f"Context:\n{context}\n\nQuestion: {query}"


_UUID_RE = re.compile(
    r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?![0-9A-Za-z])"
)


def _normalise_chunk_id(cid: str, by_lower: dict[str, str]) -> str:
    """Strip text the model appended to a UUID citation (``<uuid> (page 1)``).

    Rewrites only when the string begins with a UUID, contains no other UUID
    and that UUID is one of the supplied context ids; anything else is returned
    unchanged so a true fabrication still fails Stage 11.
    """
    if cid in by_lower.values():
        return cid
    found = list(_UUID_RE.finditer(cid))
    if len(found) != 1 or found[0].start() != len(cid) - len(cid.lstrip()):
        return cid
    return by_lower.get(found[0].group().lower(), cid)


def parse_model_output(text: str, known_ids: Collection[str] | None = None) -> dict:
    """Parse the model's JSON object, tolerating code fences and trailing text.

    Returns ``{"answer": str, "claims": [...]}``. Raises :class:`ValueError`
    when the payload is not the expected shape — the engine then falls back to
    the raw text with no claims rather than fabricating structure.

    ``known_ids`` are the chunk ids supplied in the prompt context; when given,
    a citation of the form ``<uuid> <extra text>`` is normalised to the
    matching supplied id (see :func:`_normalise_chunk_id`).
    """
    by_lower = {i.lower(): i for i in known_ids or () if i}
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
                "chunk_ids": [_normalise_chunk_id(str(cid), by_lower) for cid in chunk_ids],
            }
        )
    return {"answer": answer, "claims": claims}
