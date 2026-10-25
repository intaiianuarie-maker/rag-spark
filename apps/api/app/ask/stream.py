"""Streaming SSE endpoint for /v1/rag/stream (TASK-M4-001b).

Emits partial text tokens as they arrive from the model provider's stream,
then verifies all claims/citations at end-of-stream and emits the final
citations event. Citations are NEVER emitted mid-stream — only verified
citations appear in the final ``citations`` event.

SSE wire contract (consumed by apps/web eventsource-parser):

    event: token
    data: {"text": "<partial-text-chunk>"}

    event: citations
    data: {"citations": [...verified citations...], "status": "<answer-status>", ...}

    event: error
    data: {"error": "<reason>", "code": "<ReasonCode>"}

Error paths match the JSON POST /ask taxonomy:
  - LLM provider failure → 503 with ReasonCode.LLM_UNAVAILABLE
  - Retrieval failure    → 503 with ReasonCode.RETRIEVAL_UNAVAILABLE
  - Insufficient evidence → terminal citations event with status=insufficient_evidence
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from core.errors import ReasonCode
from fastapi import Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger("knowledge_os.api.ask.stream")


# ---------------------------------------------------------------------------
# SSE event helpers
# ---------------------------------------------------------------------------


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    """Format a dict payload as a single SSE event string with trailing newlines.

    Ensures the payload is JSON-encoded; never embeds raw model output that
    could contain ``\\n`` characters that break SSE framing unescaped.
    """
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


def _answer_to_payload(answer: Any) -> dict[str, Any]:
    """Convert an :class:`Answer` object into the SSE citations payload dict.

    This is the *only* place citations cross the wire in SSE — after end-of-stream
    verification. The payload mirrors the JSON POST /ask response shape so the
    frontend can reuse the same contract parsing.
    """
    citations = [
        c.model_dump(mode="json") if hasattr(c, "model_dump") else dict(c)
        for c in (answer.citations or [])
    ]
    claims = [
        cl.model_dump(mode="json") if hasattr(cl, "model_dump") else dict(cl)
        for cl in (answer.claims or [])
    ]
    return {
        "answer": answer.answer,
        "status": answer.status.value if hasattr(answer.status, "value") else answer.status,
        "reason_code": answer.reason_code,
        "reason": answer.reason,
        "citations": citations,
        "claims": claims,
        "confidence": answer.confidence.model_dump() if answer.confidence else None,
        "trace_id": answer.trace_id,
        "publication_status": answer.publication_status,
        "verifier_model": answer.verifier_model,
        "citation_integrity": answer.citation_integrity,
        "semantic_verification": answer.semantic_verification,
        "publication_gate": answer.publication_gate,
        "limitations": answer.limitations,
    }


async def stream_events(
    request: Request,
    engine_stream: AsyncIterator[tuple[str, Any]],
) -> AsyncIterator[str]:
    """Consume the engine's event stream and format as SSE events.

    The engine yields ``("token", text_chunk)`` tuples during generation and a
    final ``("verified", Answer)`` tuple after end-of-stream verification.

    Order guarantee: all ``token`` events precede the ``verified`` event, which
    is mapped to the terminal ``citations`` SSE event. No citation or evidence ID
    is ever emitted inside a ``token`` event — only the partial model text.
    """
    try:
        async for event_type, payload in engine_stream:
            if await request.is_disconnected():
                break
            if event_type == "token":
                yield _sse_event("token", {"text": payload})
            elif event_type == "verified":
                sse_payload = _answer_to_payload(payload)
                # Map insufficient_evidence/unavailable to error for parity with JSON path.
                status = sse_payload.get("status")
                if status == "unavailable":
                    yield _sse_event(
                        "error",
                        {
                            "error": payload.reason or "Service unavailable.",
                            "code": payload.reason_code or ReasonCode.DEPENDENCY_UNAVAILABLE.value,
                            "trace_id": payload.trace_id,
                            "citations": sse_payload.get("citations", []),
                        },
                    )
                else:
                    yield _sse_event("citations", sse_payload)
    except Exception as exc:
        # Provider/streaming errors are surfaced as an SSE error event (not a
        # raw traceback) — the caller decides the HTTP semantics.
        logger.warning(
            "stream_error",
            extra={"event": "stream_error", "error_type": type(exc).__name__},
        )
        yield _sse_event(
            "error",
            {
                "error": "The model gateway is unavailable; try again shortly.",
                "code": ReasonCode.LLM_UNAVAILABLE.value,
            },
        )


def streaming_response(
    request: Request,
    engine_stream: AsyncIterator[tuple[str, Any]],
) -> StreamingResponse:
    """Build the SSE StreamingResponse for the stream endpoint.

    Uses ``cache: no-store`` and ``X-Accel-Buffering: no`` so proxies and the
    browser flush events as they arrive rather than buffering the whole stream.
    """
    return StreamingResponse(
        stream_events(request, engine_stream=engine_stream),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
