"""Answer engine (rag-fork-solution TASK-M4-002) — the custom product glue.

Pipeline (all heavy lifting reused behind interfaces):

    orchestrator.retrieve()  ->  grounded prompt  ->  llm.generate()
                             ->  parse {answer, claims[{text, chunk_ids}]}
                             ->  verify each chunk_id (exists AND in-ACL)
                             ->  Answer(answer, claims, citations)

Guarantees:

- **Grounded**: the prompt contains only retrieved, in-scope context and
  instructs the model to copy ``chunk_id``s verbatim.
- **No fabrication**: every claim ``chunk_id`` is verified; invalid ids are
  stripped and claims left with no valid id are dropped (see
  :mod:`answer.verification`). When no claims survive verification — or the
  model produced no claims at all — the engine abstains with an
  ``INSUFFICIENT_EVIDENCE`` answer rather than echoing unverified prose.
- **Grounded rendering**: for partially-verified answers, only the text of
  verified claims is returned; unsupported sentences from the model are
  discarded.
- **Conservative extractive grounding** (v1 interim): a claim is cited only
  when its text appears **verbatim** in the cited chunk
  (:class:`~answer.verification.ExtractiveGrounder`, default). This rejects
  negation-stripping, entity/role swap and value/date changes that a
  token-overlap scorer cannot see, and never grants ``HIGH`` on paraphrase it
  cannot verify. Confidence is honest: ``HIGH`` is granted only when every
  surviving claim is grounded. **Limitation**: cross-language translation is
  *not* supported by this interim mode (a translation's text is not a substring
  of a source in another language); that is the separate multilingual release
  gate.
- **Typed failures**: an LLM provider error propagates as the provider's typed
  error so the route can map it (503); unparseable or claimless model output
  abstains with ``INSUFFICIENT_EVIDENCE`` rather than returning raw prose.
- **Honest leg statuses (T-V1-026)**: retrieval outcomes are read from the
  orchestrator's context-local report, so a **required** retrieval leg failing
  returns ``UNAVAILABLE`` with the registered safe reason and the tracking ID
  — the model is never called on an unsearched corpus, and a down service is
  never reported as "insufficient evidence". An **optional** leg (reranker)
  downgrades a standing answer to ``DEGRADED`` with the degradation disclosed.
  An **empty corpus is unchanged**: real abstention with ``EVIDENCE_MISSING``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.auth import ACLScope
from core.errors import ERROR_REGISTRY, ReasonCode
from core.logging import request_id_ctx
from models.interfaces import (
    ChatMessage,
    GenerationRequest,
    LLMProvider,
    MessageRole,
)
from retrieval.interfaces import (
    DEFAULT_CONTEXT_TOP_K,
    RetrievalMode,
    RetrievalOrchestrator,
    RetrievedItem,
)
from retrieval.orchestrator import RetrievalResult, RetrievalStatus, pop_retrieval_outcome

from .hydration import CanonicalStoreUnavailable
from .interfaces import ChunkLookup
from .models import (
    Answer,
    AnswerStatus,
    CanonicalChunk,
    Claim,
    Confidence,
    ConfidenceLevel,
    DocumentStatus,
)
from .prompts import SYSTEM_PROMPT, build_prompt, parse_model_output
from .tracing import trace_answer, trace_stream
from .verification import ClaimGrounder, ExtractiveGrounder, verify_claims

logger = logging.getLogger("knowledge_os.answer")

# ---------------------------------------------------------------------------
# Streaming answer extractor (TASK-M4-001b)
# ---------------------------------------------------------------------------
#
# The model's output is a single JSON object:
#
#     {"answer": "<text>", "claims": [{"text", "chunk_ids"}]}
#
# The SSE token seam must carry ONLY the decoded "answer" string. Raw JSON —
# with embedded unverified chunk_ids / evidence IDs — must never reach the
# client before verification. This incremental decoder tracks brace / quote
# state over the raw stream and yields decoded fragments of the "answer"
# value as they become available. Everything outside that string is ignored.
#
# The raw stream is still accumulated separately and handed to
# parse_model_output at end-of-stream for the existing verification path;
# this extractor is strictly the *presentation* side (SSE token shape).


def _decode_json_escape(raw: str) -> str:
    """Decode a slice of JSON-escaped characters.

    Handles the six short JSON escapes (``\\"`` ``\\\\`` ``\\/`` ``\\b``
    ``\\f`` ``\\n`` ``\\r`` ``\\t``) and ``\\uXXXX`` sequences. A backslash
    with no following character, or an invalid ``\\u`` hex, is passed through
    verbatim. This is best-effort presentation decode — the authoritative
    parse is :func:`answer.prompts.parse_model_output` on the full
    accumulated text.

    Invariant: for valid JSON, the decoded result equals the JSON-decoded
    value. For a raw slice that does not end on a complete escape pair, the
    trailing incomplete escape is preserved verbatim (no data loss) so a
    later ``feed`` or the end-of-stream ``json.loads`` still decodes it.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    short = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "u":
                # Need four hex digits after "u".
                if i + 6 <= n:
                    hex4 = raw[i + 2 : i + 6]
                    try:
                        out.append(chr(int(hex4, 16)))
                        i += 6
                        continue
                    except ValueError:
                        # Invalid hex — preserve the backslash verbatim and
                        # let the following characters flow through normally.
                        out.append(c)
                        i += 1
                        continue
                # Incomplete \\u escape at the tail: preserve verbatim.
                out.append(c)
                i += 1
                continue
            out.append(short.get(nxt, nxt))
            i += 2
            continue
        # Trailing lone backslash (or any other char): keep verbatim.
        out.append(c)
        i += 1
    # Combine adjacent UTF-16 surrogate pairs into single code points, matching
    # ``json.loads`` behaviour on Python 3.14+.
    combined: list[str] = []
    j = 0
    while j < len(out):
        if (
            j + 1 < len(out)
            and len(out[j]) == 1
            and 0xD800 <= ord(out[j]) <= 0xDBFF
            and len(out[j + 1]) == 1
            and 0xDC00 <= ord(out[j + 1]) <= 0xDFFF
        ):
            high = ord(out[j])
            low = ord(out[j + 1])
            combined.append(chr(0x10000 + (high - 0xD800) * 0x400 + (low - 0xDC00)))
            j += 2
        else:
            combined.append(out[j])
            j += 1
    return "".join(combined)


_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


class _StreamingAnswerExtractor:
    """Incrementally decode the model JSON's ``"answer"`` value while
    streaming.

    The decoder walks the raw stream character-by-character and tracks:

    * whether we are inside a string literal (keys and values both);
    * whether the string we are in is the top-level ``"answer"`` value;
    * JSON escape-sequence state (``\\`` and ``\\uXXXX``).

    Only characters of the ``answer`` value (once decoded) are yielded. The
    ``claims`` array, every ``chunk_ids`` list, and all surrounding JSON
    structure are never yielded.

    Escape handling across chunk boundaries: a backslash escape pair (``\\n``
    ``\\"`` ``\\uXXXX``) that straddles two provider frames is still decoded
    correctly — the escape state (``_in_escape`` / ``_u_hex``) survives the
    ``feed`` call, and the incomplete escape is held in ``_value_chars``
    verbatim until the follow-up frame completes it. No data is lost at a
    boundary; the final ``json.loads`` on the full accumulated text remains
    the authoritative parse.
    """

    def __init__(self) -> None:
        # The answer value has been found and closed; stop tracking after.
        self._closed = False
        # Whether the next opening quote starts a string that is a *key*.
        # After "{" or "," it is a key; after ":" it is a value.
        self._next_is_key = True
        # Whether we are currently inside a string literal.
        self._in_string = False
        # Whether the string we are in (if any) is the "answer" value.
        self._in_answer_value = False
        # The last key literal we closed (to know whether it was "answer").
        self._last_key = ""
        # Characters of the current string literal (raw, undecoded).
        self._string_chars: list[str] = []
        # Raw (JSON-escaped) characters of the answer value so far; flushed
        # to decoded fragments on every feed and on value-close.
        self._value_chars: list[str] = []
        # Whether the previous character started a backslash escape.
        self._in_escape = False
        # An in-progress ``\u`` hex sequence ("u" plus up to 4 hex digits).
        self._u_hex: str = ""
        # A pending high surrogate awaiting a possible low-surrogate partner.
        self._pending_surrogate: int | None = None

    def _flush_pending_surrogate(self) -> None:
        if self._pending_surrogate is not None:
            self._value_chars.append(chr(self._pending_surrogate))
            self._pending_surrogate = None

    def feed(self, chunk: str) -> list[str]:
        """Consume a raw chunk of the streamed JSON; return a list of
        non-empty *decoded* fragments of the answer string, in order.
        """
        emitted: list[str] = []
        for ch in chunk:
            self._step(ch, emitted)
        # Flush answer-value characters accumulated since the last feed so
        # the caller can surface partial text incrementally. Incomplete
        # escapes are intentionally left buffered in _in_escape/_u_hex.
        if self._in_answer_value and self._value_chars:
            frag = "".join(self._value_chars)
            self._value_chars = []
            if frag:
                emitted.append(frag)
        return emitted

    def _step(self, ch: str, emitted: list[str]) -> None:
        if self._closed:
            return
        # Inside an in-progress \\u hex sequence (the 4 hex digits).
        if self._u_hex:
            self._u_hex += ch
            if len(self._u_hex) == 5:  # "u" + 4 hex digits
                hex4 = self._u_hex[1:]
                if self._in_answer_value:
                    try:
                        cp = int(hex4, 16)
                    except ValueError:
                        # Invalid hex — preserve the raw escape verbatim.
                        self._flush_pending_surrogate()
                        self._value_chars.append("\\" + self._u_hex)
                    else:
                        if 0xD800 <= cp <= 0xDBFF:
                            # High surrogate: buffer, await a low partner.
                            self._flush_pending_surrogate()
                            self._pending_surrogate = cp
                        elif 0xDC00 <= cp <= 0xDFFF and self._pending_surrogate is not None:
                            high = self._pending_surrogate
                            self._pending_surrogate = None
                            combined = 0x10000 + (high - 0xD800) * 0x400 + (cp - 0xDC00)
                            self._value_chars.append(chr(combined))
                        else:
                            self._flush_pending_surrogate()
                            self._value_chars.append(chr(cp))
                self._u_hex = ""
            return
        # Just consumed a backslash; this char completes the escape.
        if self._in_escape:
            self._in_escape = False
            if ch == "u":
                self._u_hex = "u"
                return
            if self._in_answer_value:
                self._flush_pending_surrogate()
                if ch in _SIMPLE_ESCAPES:
                    self._value_chars.append(_SIMPLE_ESCAPES[ch])
                else:
                    # Unknown escape — preserve raw backslash + char.
                    self._value_chars.append("\\" + ch)
            return
        # Inside a string literal.
        if self._in_string:
            if ch == "\\":
                self._in_escape = True
                return
            if ch == '"':
                # String closed.
                literal = "".join(self._string_chars)
                self._string_chars = []
                self._in_string = False
                if self._in_answer_value:
                    self._flush_pending_surrogate()
                    # Flush any incomplete escape buffered (malformed JSON).
                    if self._u_hex:
                        self._value_chars.append("\\" + self._u_hex)  # type: ignore[unreachable]
                        self._u_hex = ""
                    if self._in_escape:
                        self._value_chars.append("\\")  # type: ignore[unreachable]
                        self._in_escape = False
                    frag = "".join(self._value_chars)
                    self._value_chars = []
                    if frag:
                        emitted.append(frag)
                    self._in_answer_value = False
                    self._closed = True
                else:
                    # Reset escape state so it cannot leak into the next string.
                    self._in_escape = False
                    self._u_hex = ""
                    self._pending_surrogate = None
                    # A key or a non-answer value closed.
                    if self._next_is_key:
                        self._last_key = literal
                # After a closing quote the next significant string (if any)
                # is another key (object/array) — tracked by _next_is_key.
                return
            # Regular character inside a string.
            self._string_chars.append(ch)
            if self._in_answer_value:
                self._flush_pending_surrogate()
                self._value_chars.append(ch)
            return
        # Outside a string.
        if ch.isspace():
            return
        if ch == '"':
            # Opening a string.
            self._in_string = True
            self._string_chars = []
            if not self._next_is_key:
                # It is a value. If the immediately-preceding key was the
                # top-level "answer", this is the answer value.
                if self._last_key == "answer" and not self._closed:
                    self._in_answer_value = True
                    self._value_chars = []
                    self._in_escape = False
                    self._u_hex = ""
                    self._pending_surrogate = None
            self._next_is_key = True  # after a string, the next is a key
            return
        if ch in "{,":
            # Next string is a key.
            self._next_is_key = True
            return
        if ch in "}]":
            # Next string is a value.
            self._next_is_key = False
            return
        if ch == ":":
            # The value string that follows a colon is not a key.
            self._next_is_key = False
            return
        # Any other significant character (literals, numbers).
        self._next_is_key = False
async def _record_graph_attribution(
    workspace_id: str,
    trace_id: str,
    items: list[RetrievedItem],
) -> None:
    """Write one graph reuse attribution row per answered ask (T-V2-008).

    Extracts graph element ids and scores from the retrieved items' metadata
    and scores, then writes a single row to the graph store's attribution
    table. Workspace-scoped, so cross-workspace reads are rejected. Bounded:
    one row per answer, no per-chunk fan-out. Failures are logged but do not
    fail the answer.
    """
    try:
        # Collect graph element ids from items' metadata (added by retriever)
        entity_ids: set[str] = set()
        relationship_ids: set[str] = set()
        community_ids: set[str] = set()
        scores: dict[str, float] = {}
        for item in items:
            meta = item.metadata or {}
            for eid in meta.get("graph_entity_ids", []) or []:
                entity_ids.add(str(eid))
            for rid in meta.get("graph_relationship_ids", []) or []:
                relationship_ids.add(str(rid))
            for cid in meta.get("graph_community_ids", []) or []:
                community_ids.add(str(cid))
            # Also collect chunk-level provenance and scores
            if item.chunk_id and "graph" in (item.scores or {}):
                scores[item.chunk_id] = item.scores["graph"]
            elif item.chunk_id and item.score:
                scores[item.chunk_id] = item.score

        # If no graph element ids found, fall back to chunk_ids as entity-level
        # provenance (the retriever's evidence_chunk_ids are chunk-level)
        if not entity_ids and not relationship_ids and not community_ids:
            # Use chunk_ids as the attribution's entity-equivalent
            for item in items:
                if item.chunk_id:
                    entity_ids.add(item.chunk_id)

        # Determine retrieval mode: if any graph item, it's graph-served
        retrieval_mode = "hybrid_graph" if entity_ids or relationship_ids or community_ids else "hybrid"  # noqa: E501

        # Generate answer_id as trace_id (or new UUID if trace_id is same as answer_id)
        answer_id = trace_id

        from graph.factory import graph_db_path
        from graph.sqlite_store import SQLiteGraphStore

        store = SQLiteGraphStore(graph_db_path())
        try:
            await store.record_attribution(
                workspace_id=workspace_id,
                trace_id=trace_id,
                answer_id=answer_id,
                retrieval_mode=retrieval_mode,
                entity_ids=sorted(entity_ids),
                relationship_ids=sorted(relationship_ids),
                community_ids=sorted(community_ids),
                scores=scores,
            )
        finally:
            store.close()
    except Exception as exc:
        logger.warning(
            "graph_attribution_write_failed",
            extra={
                "event": "graph_attribution_write_failed",
                "workspace_id": workspace_id,
                "trace_id": trace_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


class AnswerEngineError(RuntimeError):
    """Raised when the answer engine cannot produce an answer."""


@dataclass
class AnswerEngine:
    """Compose retrieval + generation into a verified, citation-ready answer."""

    orchestrator: RetrievalOrchestrator
    llm: LLMProvider
    model: str
    chunk_lookup: ChunkLookup | None = None
    max_context_items: int = DEFAULT_CONTEXT_TOP_K
    temperature: float | None = None
    max_tokens: int | None = None
    extra: dict = field(default_factory=dict)
    #: Optional claim-substantiation gate wired in by default
    #: (:class:`~answer.verification.ExtractiveGrounder`, conservative v1
    #: verbatim-substring check) so a citation is real *and* supports the
    #: claim. ``HIGH`` confidence is granted only when every surviving claim is
    #: grounded. Pass ``claim_grounder=None`` to restore the legacy
    #: existence+scope-only contract.
    claim_grounder: ClaimGrounder | None = field(default_factory=ExtractiveGrounder)
    #: Optional Stage 12 Gemma semantic citation verifier (T-OR-12).
    semantic_verifier: Any | None = None
    #: Verifier model identifier for Stage 12 semantic checks.
    verifier_model: str = "google/gemma-4-26b-a4b-it:free"

    @trace_answer
    async def answer(
        self, *, query: str, acl_scope: ACLScope, mode: RetrievalMode = RetrievalMode.HYBRID
    ) -> Answer:
        """Retrieve, generate, parse and verify a grounded answer.

        ``mode`` selects the retrieval leg (default ``HYBRID`` = v1 behavior);
        it is passed straight through to ``orchestrator.retrieve`` so callers
        can opt into ``GRAPH``/``HYBRID_GRAPH`` once ``GRAPH_MODE`` enables the
        graph leg (G4). ACL-before-ranking and citation verification apply to
        every mode.
        """
        try:
            items = await self.orchestrator.retrieve(
                query=query,
                acl_scope=acl_scope,
                mode=mode,
                context_top_k=self.max_context_items,
            )
        except CanonicalStoreUnavailable as exc:
            logger.warning(
                "canonical_store_unavailable",
                extra={
                        "event": "canonical_store_unavailable",
                        "error_type": type(exc).__name__,
                        "trace_id": request_id_ctx.get(),
                    },
            )
            return await self._finalize(
                Answer(
                    answer="I cannot answer this question right now.",
                    status=AnswerStatus.UNAVAILABLE,
                    reason_code=ReasonCode.DEPENDENCY_UNAVAILABLE.value,
                    reason="Canonical chunk store unavailable during hydration.",
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        basis="Store unavailable during canonical hydration",
                    ),
                    trace_id=request_id_ctx.get(),
                    claims=[],
                    citations=[],
                ),
                items=[],
                outcome=None,
            )
        # T-V1-026: the orchestrator published the honest outcome of that call
        # on a context-local slot (works through the hydration decorator and
        # through test doubles, which publish nothing and read as healthy).
        outcome: RetrievalResult | None = pop_retrieval_outcome()
        trace_id = request_id_ctx.get() or (outcome.trace_id if outcome else None)

        if outcome is not None and outcome.status is RetrievalStatus.UNAVAILABLE:
            # A REQUIRED leg failed: the corpus was never searched, so
            # "insufficient evidence" would be a lie. Fail closed with the
            # registered safe reason + tracking ID and never call the model on
            # an empty context.
            reason_code = (
                outcome.reason_code.value if outcome.reason_code is not None else None
            )
            logger.warning(
                "retrieval_unavailable",
                extra={
                    "event": "retrieval_unavailable",
                    "reason_code": reason_code,
                    "trace_id": trace_id,
                    "legs_failed": [failure.leg for failure in outcome.legs_failed],
                },
            )
            return await self._finalize(
                Answer(
                    answer="I cannot answer this question right now.",
                    status=AnswerStatus.UNAVAILABLE,
                    reason_code=reason_code,
                    reason=outcome.reason,
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        basis="A required retrieval leg was unavailable; nothing was searched",
                    ),
                    trace_id=trace_id,
                    claims=[],
                    citations=[],
                ),
                items=items,
                outcome=outcome,
            )

        prompt = build_prompt(query, items)
        request = GenerationRequest(
            model=self.model,
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=prompt),
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        try:
            result = await self.llm.generate(request)
        except Exception as exc:
            # Generation leg: never swallow into an abstention. The typed
            # provider error propagates so the route maps it to 503 with the
            # same tracking ID this log line carries.
            logger.warning(
                "generation_leg_failed",
                extra={
                    "event": "generation_leg_failed",
                    "error_type": type(exc).__name__,
                    "trace_id": trace_id,
                },
            )
            raise

        try:
            parsed = parse_model_output(result.text)
        except ValueError:
            # Never fabricate structure or return raw prose as a factual answer.
            # T-V1-051: log the registry's safe PARSE_FALLBACK message (never
            # str(exc), which can carry internals) alongside the tracking ID;
            # the Answer below keeps its fixed safe reason string.
            logger.warning(
                "answer_parse_fallback",
                extra={
                    "event": "answer_parse_fallback",
                    "reason": ERROR_REGISTRY[ReasonCode.PARSE_FALLBACK].message,
                    "trace_id": trace_id,
                },
            )
            return await self._finalize(
                Answer(
                    answer="I cannot answer this question based on the provided documents.",
                    status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                    reason_code="PARSE_FALLBACK",
                    reason="Model output was not valid JSON; no claims could be verified.",
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        basis="Model output unparseable — no structure to verify",
                    ),
                    trace_id=trace_id,
                    claims=[],
                    citations=[],
                ),
                items=items,
                outcome=outcome,
            )

        claims = [Claim(text=c["text"], chunk_ids=c["chunk_ids"]) for c in parsed["claims"]]
        allowed: set[str] | None = set(acl_scope.workspace_ids)
        if acl_scope.include_public:
            allowed = None  # public scope: existence is the only check

        # Stage 11 — server-side deterministic citation integrity validator (T-OR-05 / T-OR-12)
        stage11_report = None
        try:
            from .citation_integrity import validate_citation_integrity

            canonical_from_items = {
                item.chunk_id: CanonicalChunk(
                    chunk_id=item.chunk_id,
                    workspace_id=item.workspace_id,
                    document_id=item.document_id,
                    document_version_id=item.document_version_id,
                    filename=(item.metadata or {}).get("filename"),
                    page=item.page,
                    section=item.section,
                    content=item.content,
                    status=DocumentStatus.ACTIVE,
                    metadata=dict(item.metadata or {}),
                )
                for item in items
                if item.chunk_id
                and not (item.metadata or {}).get("is_graph")
                and not (item.metadata or {}).get("routing_hint")
            }

            stage11_report = await validate_citation_integrity(
                claims=claims,
                items=items,
                canonical=canonical_from_items,
                chunk_lookup=self.chunk_lookup,
                allowed_workspace_ids=allowed,
            )
        except Exception as exc:
            logger.warning(
                "stage11_integrity_check_failed",
                extra={"event": "stage11_integrity_check_failed", "error_type": type(exc).__name__},
            )

        try:
            verified, citations = await verify_claims(
                claims,
                items,
                chunk_lookup=self.chunk_lookup,
                allowed_workspace_ids=allowed,
                claim_grounder=self.claim_grounder,
            )
        except CanonicalStoreUnavailable as exc:
            logger.warning(
                "canonical_store_unavailable",
                extra={
                    "event": "canonical_store_unavailable",
                    "error_type": type(exc).__name__,
                    "trace_id": request_id_ctx.get(),
                },
            )
            return await self._finalize(
                Answer(
                    answer="I cannot answer this question right now.",
                    status=AnswerStatus.UNAVAILABLE,
                    reason_code=ReasonCode.DEPENDENCY_UNAVAILABLE.value,
                    reason="Canonical chunk store unavailable during verification.",
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        basis="Store unavailable during canonical verification",
                    ),
                    trace_id=request_id_ctx.get(),
                    claims=[],
                    citations=[],
                    citation_integrity=stage11_report.model_dump() if stage11_report else None,
                ),
                items=items,
                outcome=outcome if "outcome" in locals() else None,
            )

        answer_text = parsed["answer"].strip()
        lower_ans = answer_text.lower()
        is_refusal_phrase = any(
            phrase in lower_ans
            for phrase in (
                "does not contain",
                "cannot answer",
                "nu conține",
                "não contém",
                "insufficient information",
                "not enough information",
            )
        )

        if not items or is_refusal_phrase:
            return await self._finalize(
                Answer(
                    answer="I cannot answer this question based on the provided documents.",
                    status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                    reason_code="EVIDENCE_MISSING",
                    reason="Accessible sources do not contain sufficient evidence to answer.",
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        basis="No relevant documents retrieved or model declared missing evidence",
                    ),
                    trace_id=trace_id,
                    claims=verified,
                    citations=citations,
                    citation_integrity=stage11_report.model_dump() if stage11_report else None,
                ),
                items=items,
                outcome=outcome,
            )

        if not claims:
            # No structured claims produced by model — cannot verify anything.
            return await self._finalize(
                Answer(
                    answer="I cannot answer this question based on the provided documents.",
                    status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                    reason_code="NO_CLAIMS",
                    reason=(
                        "Model response did not include verifiable claims "
                        "grounded in citations."
                    ),
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        basis="No claims produced",
                    ),
                    trace_id=trace_id,
                    claims=[],
                    citations=[],
                    citation_integrity=stage11_report.model_dump() if stage11_report else None,
                ),
                items=items,
                outcome=outcome,
            )

        if self.semantic_verifier is None and claims and not verified:
            return await self._finalize(
                Answer(
                    answer="I cannot answer this question based on the provided documents.",
                    status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                    reason_code="CITATION_INVALID",
                    reason="Generated claims lacked valid citations in accessible documents.",
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        basis="Zero verified claims survived citation integrity check",
                    ),
                    trace_id=trace_id,
                    claims=[],
                    citations=[],
                    citation_integrity=stage11_report.model_dump() if stage11_report else None,
                ),
                items=items,
                outcome=outcome,
            )

        # Stage 12 Gemma semantic verifier & publication gate (T-OR-06 / T-OR-12)
        stage12_report = None
        gate_result = None
        if self.semantic_verifier is not None and claims and stage11_report is not None:
            from .semantic_verification import (
                PublicationStatus,
                Verdict,
                evaluate_publication_gate,
                verify_semantic_claims,
            )

            stage12_report = await verify_semantic_claims(
                claims=claims,
                integrity_report=stage11_report,
                verifier=self.semantic_verifier,
            )
            verifier_model_id = getattr(self.semantic_verifier, "_model", self.verifier_model)
            gate_result = await evaluate_publication_gate(
                claims=claims,
                integrity_report=stage11_report,
                semantic_report=stage12_report,
                chunk_lookup=self.chunk_lookup,
                verifier_model=verifier_model_id,
            )

            if gate_result.can_publish:
                verified = list(claims)
                citations = list(
                    stage11_report.accepted_citations
                    if stage11_report.accepted_citations
                    else citations
                )

            if not gate_result.can_publish:
                gate_reasons = "; ".join(gate_result.reasons)
                if gate_result.status == PublicationStatus.REJECTED:
                    return await self._finalize(
                        Answer(
                            answer="I cannot answer this question based on the provided documents.",
                            status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                            reason_code="CITATION_INVALID",
                            reason=f"Citation integrity verification failed: {gate_reasons}",
                            confidence=Confidence(
                                level=ConfidenceLevel.LOW,
                                basis="Stage 11 integrity check failed",
                            ),
                            trace_id=trace_id,
                            claims=[],
                            citations=[],
                            citation_integrity=stage11_report.model_dump(),
                            semantic_verification=stage12_report.model_dump(),
                            publication_gate=gate_result.model_dump(),
                            publication_status=gate_result.status.value,
                            verifier_model=gate_result.verifier_model,
                        ),
                        items=items,
                        outcome=outcome,
                    )
                elif gate_result.status == PublicationStatus.ABSTAIN:
                    reason_code = (
                        "CONTRADICTION_FOUND"
                        if any(
                            cv.verdict == Verdict.CONTRADICTED
                            for cv in stage12_report.claim_verdicts
                        )
                        else "EVIDENCE_MISSING"
                    )
                    return await self._finalize(
                        Answer(
                            answer="I cannot answer this question based on the provided documents.",
                            status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                            reason_code=reason_code,
                            reason=(
                                f"Semantic claims contradicted or unsupported: {gate_reasons}"
                            ),
                            confidence=Confidence(
                                level=ConfidenceLevel.LOW,
                                basis="Semantic verification abstained on candidate claims",
                            ),
                            trace_id=trace_id,
                            claims=[],
                            citations=[],
                            citation_integrity=stage11_report.model_dump(),
                            semantic_verification=stage12_report.model_dump(),
                            publication_gate=gate_result.model_dump(),
                            publication_status=gate_result.status.value,
                            verifier_model=gate_result.verifier_model,
                        ),
                        items=items,
                        outcome=outcome,
                    )
                elif gate_result.status == PublicationStatus.NEEDS_REVIEW:
                    rendered_answer = (
                        " ".join(claim.text for claim in verified)
                        if verified
                        else answer_text
                    )
                    return await self._finalize(
                        Answer(
                            answer=rendered_answer,
                            status=AnswerStatus.NEEDS_REVIEW,
                            reason_code=ReasonCode.NEEDS_REVIEW.value,
                            reason=(
                                f"Semantic verification requires human review: {gate_reasons}"
                            ),
                            confidence=Confidence(
                                level=ConfidenceLevel.LOW,
                                basis="Semantic verification uncertain or partially supported",
                            ),
                            trace_id=trace_id,
                            claims=verified,
                            citations=citations,
                            citation_integrity=stage11_report.model_dump(),
                            semantic_verification=stage12_report.model_dump(),
                            publication_gate=gate_result.model_dump(),
                            publication_status=gate_result.status.value,
                            verifier_model=gate_result.verifier_model,
                        ),
                        items=items,
                        outcome=outcome,
                    )

        if claims and len(verified) < len(claims):
            # Partial verification: render only from the verified claims.
            rendered_answer = " ".join(claim.text for claim in verified)
            pub_status = (
                gate_result.status.value
                if gate_result
                else ("verified" if verified else None)
            )
            return await self._finalize(
                Answer(
                    answer=rendered_answer,
                    status=AnswerStatus.PARTIAL,
                    reason_code="PARTIAL_EVIDENCE",
                    reason=(
                        f"Only {len(verified)} of {len(claims)} "
                        "claim(s) could be verified with valid citations."
                    ),
                    confidence=Confidence(
                        level=ConfidenceLevel.MEDIUM,
                        basis=(
                            f"Partial verification: "
                            f"{len(verified)}/{len(claims)} claim(s) supported"
                        ),
                    ),
                    trace_id=trace_id,
                    claims=verified,
                    citations=citations,
                    citation_integrity=stage11_report.model_dump() if stage11_report else None,
                    semantic_verification=stage12_report.model_dump() if stage12_report else None,
                    publication_gate=gate_result.model_dump() if gate_result else None,
                    publication_status=pub_status,
                    verifier_model=gate_result.verifier_model if gate_result else None,
                ),
                items=items,
                outcome=outcome,
            )

        has_high_conf = bool(citations) and (len(verified) == len(claims) if claims else False)
        confidence_level = ConfidenceLevel.HIGH if has_high_conf else ConfidenceLevel.MEDIUM
        rendered_answer = " ".join(claim.text for claim in verified)
        pub_status = (
            gate_result.status.value
            if gate_result
            else ("verified" if citations else None)
        )
        return await self._finalize(
            Answer(
                answer=rendered_answer,
                status=AnswerStatus.ANSWERED,
                reason_code=None,
                reason=None,
                confidence=Confidence(
                    level=confidence_level,
                    basis=(
                        f"Answer grounded in {len(citations)} "
                        "verified, substantiated citation(s)"
                    ),
                ),
                trace_id=trace_id,
                claims=verified,
                citations=citations,
                citation_integrity=stage11_report.model_dump() if stage11_report else None,
                semantic_verification=stage12_report.model_dump() if stage12_report else None,
                publication_gate=gate_result.model_dump() if gate_result else None,
                publication_status=pub_status,
                verifier_model=gate_result.verifier_model if gate_result else None,
            ),
            items=items,
            outcome=outcome,
        )

    @staticmethod
    def _disclose(answer: Answer, outcome: RetrievalResult | None) -> Answer:
        """Downgrade a standing answer to ``degraded`` when an OPTIONAL leg was down.

        T-V1-026: the answer still stands, but the user must be told the
        reranker (or another optional capability) was unavailable. The reason
        code is kept inside the set registered for ``degraded``
        (``RERANK_UNAVAILABLE`` / ``DEGRADED_RESULT``); per-leg codes such as
        ``GRAPH_UNAVAILABLE`` remain observable on the retrieval side.
        """
        if outcome is None or outcome.status is not RetrievalStatus.DEGRADED:
            return answer
        code = outcome.reason_code
        if code is None or code.value not in {"DEGRADED_RESULT", "RERANK_UNAVAILABLE"}:
            code = ReasonCode.DEGRADED_RESULT
        return answer.model_copy(
            update={
                "status": AnswerStatus.DEGRADED,
                "reason_code": code.value,
                "reason": outcome.reason,
            }
        )

    # --- lost-region disclosure (T-V1-031; made complete by T-V1-052) ------

    @staticmethod
    def _collect_lost_regions(items: Sequence[RetrievedItem]) -> list[dict]:
        """Lost regions carried by the retrieved chunks (T-V1-031).

        The ingestion pipeline writes them onto every chunk of a partially-read
        source (``ingest_pipeline.parse_disclosure``); retrieval hands them
        back on the items' metadata unchanged.
        """
        lost: list[dict] = []
        for item in items:
            for region in item.metadata.get("parse_lost_regions") or ():
                if isinstance(region, dict) and region not in lost:
                    lost.append(region)
        return lost

    @staticmethod
    def _limitation_text(lost: Sequence[dict]) -> str:
        page_summary = "; ".join(
            f"pages {region.get('lost_pages')} of {region.get('page_count')}"
            for region in lost
        )
        return (
            f"Only part of the source was read: {page_summary} carried an image "
            "and no text layer, so that content was NOT available to this answer."
        )

    @classmethod
    def _disclose_source(cls, answer: Answer, items: Sequence[RetrievedItem]) -> Answer:
        """Disclose partially-parsed sources; never let one read as confident (T-V1-031).

        The pipeline parks a job ``needs_review`` when part of a source could not
        be read (e.g. image-only PDF pages) and writes the lost regions onto
        every chunk it published. Retrieval hands that metadata straight back on
        the retrieved items, so the answer layer says the same thing the job
        said — instead of answering over an omitted region as if the document
        had been read in full.

        Two rules, both hard:

        * a **standing** answer is labelled ``needs_review`` and lists the
          regions that were never read (an abstention keeps its own reason —
          it is already not a claim to have read anything);
        * confidence is capped at ``MEDIUM``. ``HIGH`` claims completeness, and
          completeness is exactly what a partially-read source does not have.
        """
        lost = cls._collect_lost_regions(items)
        if not lost:
            return answer

        limitation = cls._limitation_text(lost)
        limitations = [*answer.limitations]
        if limitation not in limitations:
            limitations.append(limitation)

        level = (
            ConfidenceLevel.MEDIUM
            if answer.confidence.level is ConfidenceLevel.HIGH
            else answer.confidence.level
        )
        standing = answer.status in {AnswerStatus.ANSWERED, AnswerStatus.PARTIAL}
        return answer.model_copy(
            update={
                "status": AnswerStatus.NEEDS_REVIEW if standing else answer.status,
                "reason_code": (
                    ReasonCode.NEEDS_REVIEW.value if standing else answer.reason_code
                ),
                "reason": (
                    "A cited source was only partially parsed; see limitations."
                    if standing
                    else answer.reason
                ),
                "limitations": limitations,
                "confidence": Confidence(
                    level=level,
                    basis=(
                        f"{answer.confidence.basis} — capped at {level.value}: a "
                        "partially-parsed source cannot ground a complete answer"
                    ),
                ),
            }
        )

    async def _finalize(
        self,
        answer: Answer,
        *,
        items: Sequence[RetrievedItem],
        outcome: RetrievalResult | None,
    ) -> Answer:
        """The single funnel every ``answer()`` return path goes through (T-V1-052).

        Before this ticket the lost-region disclosure was applied to the two
        standing returns only, so the abstention returns (``PARSE_FALLBACK``,
        ``EVIDENCE_MISSING``, ``NO_CLAIMS``, ``CITATION_INVALID``) and the
        fail-closed ``UNAVAILABLE`` return answered over a partially-read
        source without saying so. Every return now funnels through here, so
        **undisclosing return paths: 0**.

        Order matters and is deliberate:

        1. :meth:`_disclose_source` first — it appends the lost-region
           limitation and caps confidence, and relabels a *standing* answer
           ``needs_review``;
        2. :meth:`_disclose` second — an optional leg being down may still
           override the status to ``degraded`` (T-V1-026's registered set),
           but the lost-region **limitation is already appended and the
           confidence already capped**, so nothing is lost: the user reads
           both facts even when only one of them can own the status field.

        Abstentions keep their own ``reason_code``/``reason`` —
        :meth:`_disclose_source` only relabels standing answers — and still
        disclose.

        T-V2-008: graph reuse attribution (one row per answered ask, bounded).
        After disclosure, the answer's trace_id and the retrieved graph items'
        provenance are used to write a single attribution row per answer, if
        graph-served. The row is workspace-scoped and queryable for eval
        (e.g. fraction of answers that touched graph). Failures to write the
        row are logged but do not fail the answer (bounded write, not per-chunk
        fan-out).
        """
        # T-V2-008: record graph reuse attribution if this answer was graph-served.
        # This is the single call site that emits the row (hunks confined here).
        # We await the write here (one row per answer, no per-chunk fan-out) so
        # the row is durable before the answer returns - every graph-served answer
        # has a row, and tests can assert without racing a background task.
        try:
            graph_items = [item for item in items if (item.metadata or {}).get("is_graph")]
            if graph_items and answer.trace_id:
                ws_id = graph_items[0].workspace_id
                if ws_id:
                    try:
                        await _record_graph_attribution(
                            workspace_id=ws_id,
                            trace_id=answer.trace_id,
                            items=graph_items,
                        )
                    except Exception as exc:
                        logger.warning(
                            "graph_attribution_write_failed",
                            extra={
                                "event": "graph_attribution_write_failed",
                                "workspace_id": ws_id,
                                "trace_id": answer.trace_id,
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                        )
        except Exception as exc:
            logger.warning(
                "graph_attribution_lost",
                extra={
                    "event": "graph_attribution_lost",
                    "error_type": type(exc).__name__,
                },
            )

        return self._disclose(self._disclose_source(answer, items), outcome)

    # ------------------------------------------------------------------
    # Streaming variant (TASK-M4-001b)
    # ------------------------------------------------------------------

    @trace_stream
    async def stream(
        self, *, query: str, acl_scope: ACLScope, mode: RetrievalMode = RetrievalMode.HYBRID
    ) -> AsyncIterator[tuple[str, Any]]:
        """Yield ``("token", text_chunk)`` events then a final ``("verified", answer)`` event.

        The streaming protocol:

        * ``("token", "<text>")`` — raw text deltas from the model, emitted as
          they arrive. **No citation or evidence ID is ever emitted inside a
          token event** — only partial response text.
        * ``("verified", Answer)`` — the final event, emitted exactly once.
          By the time it fires, the accumulated text has been parsed and
          **every claim has been run through ``verify_claims``** so the
          :class:`Answer` carries only verified, in-scope citations. No
          unverified IDs ever cross this seam.

        This keeps the existing JSON path's verification contract (existence +
        scope + grounding + Stage 11/12 gate) while supporting SSE transport.
        """
        trace_id = request_id_ctx.get()

        # --- Retrieval (same contract as .answer) -------------------------
        try:
            items = await self.orchestrator.retrieve(
                query=query,
                acl_scope=acl_scope,
                mode=mode,
                context_top_k=self.max_context_items,
            )
        except CanonicalStoreUnavailable as exc:
            logger.warning(
                "canonical_store_unavailable",
                extra={
                    "event": "canonical_store_unavailable",
                    "error_type": type(exc).__name__,
                    "trace_id": trace_id,
                },
            )
            yield (
                "verified",
                Answer(
                    answer="I cannot answer this question right now.",
                    status=AnswerStatus.UNAVAILABLE,
                    reason_code=ReasonCode.DEPENDENCY_UNAVAILABLE.value,
                    reason="Canonical chunk store unavailable during hydration.",
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        basis="Store unavailable during canonical hydration",
                    ),
                    trace_id=trace_id,
                    claims=[],
                    citations=[],
                ),
            )
            return

        from retrieval.orchestrator import (
            RetrievalStatus,
            pop_retrieval_outcome,
        )

        outcome: RetrievalResult | None = pop_retrieval_outcome()
        trace_id = trace_id or (outcome.trace_id if outcome else None)

        if outcome is not None and outcome.status is RetrievalStatus.UNAVAILABLE:
            reason_code = (
                outcome.reason_code.value if outcome.reason_code is not None else None
            )
            logger.warning(
                "retrieval_unavailable",
                extra={
                    "event": "retrieval_unavailable",
                    "reason_code": reason_code,
                    "trace_id": trace_id,
                    "legs_failed": [failure.leg for failure in outcome.legs_failed],
                },
            )
            yield (
                "verified",
                Answer(
                    answer="I cannot answer this question right now.",
                    status=AnswerStatus.UNAVAILABLE,
                    reason_code=reason_code,
                    reason=outcome.reason,
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        basis="A required retrieval leg was unavailable; nothing was searched",
                    ),
                    trace_id=trace_id,
                    claims=[],
                    citations=[],
                ),
            )
            return

        # --- Generation (streaming) ---------------------------------------
        prompt = build_prompt(query, items)
        request = GenerationRequest(
            model=self.model,
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=prompt),
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        accumulated: list[str] = []
        # Streaming answer-text extractor — only yields the "answer" value,
        # never any chunk_ids or claims structure.
        extractor = _StreamingAnswerExtractor()
        model_stream = self.llm.stream(request)
        try:
            async for chunk in model_stream:
                accumulated.append(chunk)
                # Emit partial answer text as token events as it arrives.
                for fragment in extractor.feed(chunk):
                    yield ("token", fragment)
        except Exception as exc:
            logger.warning(
                "generation_leg_failed",
                extra={
                    "event": "generation_leg_failed",
                    "error_type": type(exc).__name__,
                    "trace_id": trace_id,
                },
            )
            raise
        finally:
            close = getattr(model_stream, "aclose", None)
            if close is not None:
                await close()

        raw_text = "".join(accumulated)

        # --- Parse + Verify (same contract as .answer) --------------------
        try:
            parsed = parse_model_output(raw_text)
        except ValueError:
            logger.warning(
                "answer_parse_fallback",
                extra={
                    "event": "answer_parse_fallback",
                    "reason": ERROR_REGISTRY[ReasonCode.PARSE_FALLBACK].message,
                    "trace_id": trace_id,
                },
            )
            yield (
                "verified",
                await self._finalize(
                    Answer(
                        answer="I cannot answer this question based on the provided documents.",
                        status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                        reason_code="PARSE_FALLBACK",
                        reason="Model output was not valid JSON; no claims could be verified.",
                        confidence=Confidence(
                            level=ConfidenceLevel.LOW,
                            basis="Model output unparseable — no structure to verify",
                        ),
                        trace_id=trace_id,
                        claims=[],
                        citations=[],
                    ),
                    items=items,
                    outcome=outcome,
                ),
            )
            return

        if not raw_text or not parsed.get("claims"):
            yield (
                "verified",
                await self._finalize(
                    Answer(
                        answer="I cannot answer this question based on the provided documents.",
                        status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                        reason_code="NO_CLAIMS",
                        reason="Model response did not include verifiable "
                        "claims grounded in citations.",
                        confidence=Confidence(
                            level=ConfidenceLevel.LOW,
                            basis="No claims produced",
                        ),
                        trace_id=trace_id,
                        claims=[],
                        citations=[],
                    ),
                    items=items,
                    outcome=outcome,
                ),
            )
            return

        # --- Verification (mirrors answer(), lines 333-680) -------------------
        claims = [Claim(text=c["text"], chunk_ids=c["chunk_ids"]) for c in parsed["claims"]]
        allowed: set[str] | None = set(acl_scope.workspace_ids)
        if acl_scope.include_public:
            allowed = None  # public scope: existence is the only check

        # Stage 11 — server-side deterministic citation integrity validator
        stage11_report = None
        try:
            canonical_from_items = {
                item.chunk_id: CanonicalChunk(
                    chunk_id=item.chunk_id,
                    workspace_id=item.workspace_id,
                    document_id=item.document_id,
                    document_version_id=item.document_version_id,
                    filename=(item.metadata or {}).get("filename"),
                    page=item.page,
                    section=item.section,
                    content=item.content,
                    status=DocumentStatus.ACTIVE,
                    metadata=dict(item.metadata or {}),
                )
                for item in items
                if item.chunk_id
                and not (item.metadata or {}).get("is_graph")
                and not (item.metadata or {}).get("routing_hint")
            }

            from .citation_integrity import validate_citation_integrity

            stage11_report = await validate_citation_integrity(
                claims=claims,
                items=items,
                canonical=canonical_from_items,
                chunk_lookup=self.chunk_lookup,
                allowed_workspace_ids=allowed,
            )
        except Exception as exc:
            logger.warning(
                "stage11_integrity_check_failed",
                extra={"event": "stage11_integrity_check_failed", "error_type": type(exc).__name__},
            )

        try:
            verified, citations = await verify_claims(
                claims,
                items,
                chunk_lookup=self.chunk_lookup,
                allowed_workspace_ids=allowed,
                claim_grounder=self.claim_grounder,
            )
        except CanonicalStoreUnavailable as exc:
            logger.warning(
                "canonical_store_unavailable",
                extra={
                    "event": "canonical_store_unavailable",
                    "error_type": type(exc).__name__,
                    "trace_id": trace_id,
                },
            )
            yield (
                "verified",
                await self._finalize(
                    Answer(
                        answer="I cannot answer this question right now.",
                        status=AnswerStatus.UNAVAILABLE,
                        reason_code=ReasonCode.DEPENDENCY_UNAVAILABLE.value,
                        reason="Canonical chunk store unavailable during verification.",
                        confidence=Confidence(
                            level=ConfidenceLevel.LOW,
                            basis="Store unavailable during canonical verification",
                        ),
                        trace_id=trace_id,
                        claims=[],
                        citations=[],
                        citation_integrity=stage11_report.model_dump() if stage11_report else None,
                    ),
                    items=items,
                    outcome=outcome,
                ),
            )
            return

        answer_text = parsed["answer"].strip()
        lower_ans = answer_text.lower()
        is_refusal_phrase = any(
            phrase in lower_ans
            for phrase in (
                "does not contain",
                "cannot answer",
                "nu conține",
                "não contém",
                "insufficient information",
                "not enough information",
            )
        )

        if not items or is_refusal_phrase:
            yield (
                "verified",
                await self._finalize(
                    Answer(
                        answer="I cannot answer this question based on the provided documents.",
                        status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                        reason_code="EVIDENCE_MISSING",
                        reason="Accessible sources do not contain sufficient evidence to answer.",
                        confidence=Confidence(
                            level=ConfidenceLevel.LOW,
                            basis="No relevant documents retrieved or model "
                            "declared missing evidence",
                        ),
                        trace_id=trace_id,
                        claims=verified,
                        citations=citations,
                        citation_integrity=stage11_report.model_dump() if stage11_report else None,
                    ),
                    items=items,
                    outcome=outcome,
                ),
            )
            return

        if not claims:
            yield (
                "verified",
                await self._finalize(
                    Answer(
                        answer="I cannot answer this question based on the provided documents.",
                        status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                        reason_code="NO_CLAIMS",
                        reason=(
                            "Model response did not include verifiable claims "
                            "grounded in citations."
                        ),
                        confidence=Confidence(
                            level=ConfidenceLevel.LOW,
                            basis="No claims produced",
                        ),
                        trace_id=trace_id,
                        claims=[],
                        citations=[],
                        citation_integrity=stage11_report.model_dump() if stage11_report else None,
                    ),
                    items=items,
                    outcome=outcome,
                ),
            )
            return

        if self.semantic_verifier is None and claims and not verified:
            yield (
                "verified",
                await self._finalize(
                    Answer(
                        answer="I cannot answer this question based on the provided documents.",
                        status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                        reason_code="CITATION_INVALID",
                        reason="Generated claims lacked valid citations in accessible documents.",
                        confidence=Confidence(
                            level=ConfidenceLevel.LOW,
                            basis="Zero verified claims survived citation integrity check",
                        ),
                        trace_id=trace_id,
                        claims=[],
                        citations=[],
                        citation_integrity=stage11_report.model_dump() if stage11_report else None,
                    ),
                    items=items,
                    outcome=outcome,
                ),
            )
            return

        # Stage 12 Gemma semantic verifier & publication gate
        stage12_report = None
        gate_result = None
        if self.semantic_verifier is not None and claims and stage11_report is not None:
            from .semantic_verification import (
                PublicationStatus,
                Verdict,
                evaluate_publication_gate,
                verify_semantic_claims,
            )

            stage12_report = await verify_semantic_claims(
                claims=claims,
                integrity_report=stage11_report,
                verifier=self.semantic_verifier,
            )
            verifier_model_id = getattr(self.semantic_verifier, "_model", self.verifier_model)
            gate_result = await evaluate_publication_gate(
                claims=claims,
                integrity_report=stage11_report,
                semantic_report=stage12_report,
                chunk_lookup=self.chunk_lookup,
                verifier_model=verifier_model_id,
            )

            if gate_result.can_publish:
                verified = list(claims)
                citations = list(
                    stage11_report.accepted_citations
                    if stage11_report.accepted_citations
                    else citations
                )

            if not gate_result.can_publish:
                gate_reasons = "; ".join(gate_result.reasons)
                if gate_result.status == PublicationStatus.REJECTED:
                    yield (
                        "verified",
                        await self._finalize(
                            Answer(
                                answer="I cannot answer this question "
                                "based on the provided documents.",
                                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                                reason_code="CITATION_INVALID",
                                reason=f"Citation integrity verification failed: {gate_reasons}",
                                confidence=Confidence(
                                    level=ConfidenceLevel.LOW,
                                    basis="Stage 11 integrity check failed",
                                ),
                                trace_id=trace_id,
                                claims=[],
                                citations=[],
                                citation_integrity=stage11_report.model_dump(),
                                semantic_verification=stage12_report.model_dump(),
                                publication_gate=gate_result.model_dump(),
                                publication_status=gate_result.status.value,
                                verifier_model=gate_result.verifier_model,
                            ),
                            items=items,
                            outcome=outcome,
                        ),
                    )
                    return
                elif gate_result.status == PublicationStatus.ABSTAIN:
                    reason_code = (
                        "CONTRADICTION_FOUND"
                        if any(
                            cv.verdict == Verdict.CONTRADICTED
                            for cv in stage12_report.claim_verdicts
                        )
                        else "EVIDENCE_MISSING"
                    )
                    yield (
                        "verified",
                        await self._finalize(
                            Answer(
                                answer="I cannot answer this question "
                                "based on the provided documents.",
                                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                                reason_code=reason_code,
                                reason=(
                                    f"Semantic claims contradicted or unsupported: {gate_reasons}"
                                ),
                                confidence=Confidence(
                                    level=ConfidenceLevel.LOW,
                                    basis="Semantic verification abstained on candidate claims",
                                ),
                                trace_id=trace_id,
                                claims=[],
                                citations=[],
                                citation_integrity=stage11_report.model_dump(),
                                semantic_verification=stage12_report.model_dump(),
                                publication_gate=gate_result.model_dump(),
                                publication_status=gate_result.status.value,
                                verifier_model=gate_result.verifier_model,
                            ),
                            items=items,
                            outcome=outcome,
                        ),
                    )
                    return
                elif gate_result.status == PublicationStatus.NEEDS_REVIEW:
                    rendered_answer = (
                        " ".join(claim.text for claim in verified)
                        if verified
                        else answer_text
                    )
                    yield (
                        "verified",
                        await self._finalize(
                            Answer(
                                answer=rendered_answer,
                                status=AnswerStatus.NEEDS_REVIEW,
                                reason_code=ReasonCode.NEEDS_REVIEW.value,
                                reason=(
                                    f"Semantic verification requires human review: {gate_reasons}"
                                ),
                                confidence=Confidence(
                                    level=ConfidenceLevel.LOW,
                                    basis="Semantic verification uncertain or partially supported",
                                ),
                                trace_id=trace_id,
                                claims=verified,
                                citations=citations,
                                citation_integrity=stage11_report.model_dump(),
                                semantic_verification=stage12_report.model_dump(),
                                publication_gate=gate_result.model_dump(),
                                publication_status=gate_result.status.value,
                                verifier_model=gate_result.verifier_model,
                            ),
                            items=items,
                            outcome=outcome,
                        ),
                    )
                    return

        if claims and len(verified) < len(claims):
            rendered_answer = " ".join(claim.text for claim in verified)
            pub_status = (
                gate_result.status.value
                if gate_result
                else ("verified" if verified else None)
            )
            yield (
                "verified",
                await self._finalize(
                    Answer(
                        answer=rendered_answer,
                        status=AnswerStatus.PARTIAL,
                        reason_code="PARTIAL_EVIDENCE",
                        reason=(
                            f"Only {len(verified)} of {len(claims)} "
                            "claim(s) could be verified with valid citations."
                        ),
                        confidence=Confidence(
                            level=ConfidenceLevel.MEDIUM,
                            basis=(
                                f"Partial verification: "
                                f"{len(verified)}/{len(claims)} claim(s) supported"
                            ),
                        ),
                        trace_id=trace_id,
                        claims=verified,
                        citations=citations,
                        citation_integrity=stage11_report.model_dump() if stage11_report else None,
                        semantic_verification=(
                            stage12_report.model_dump() if stage12_report else None
                        ),
                        publication_gate=gate_result.model_dump() if gate_result else None,
                        publication_status=pub_status,
                        verifier_model=gate_result.verifier_model if gate_result else None,
                    ),
                    items=items,
                    outcome=outcome,
                ),
            )
            return

        has_high_conf = bool(citations) and (len(verified) == len(claims) if claims else False)
        confidence_level = ConfidenceLevel.HIGH if has_high_conf else ConfidenceLevel.MEDIUM
        rendered_answer = " ".join(claim.text for claim in verified)
        pub_status = (
            gate_result.status.value
            if gate_result
            else ("verified" if citations else None)
        )
        yield (
            "verified",
            await self._finalize(
                Answer(
                    answer=rendered_answer,
                    status=AnswerStatus.ANSWERED,
                    reason_code=None,
                    reason=None,
                    confidence=Confidence(
                        level=confidence_level,
                        basis=(
                            f"Answer grounded in {len(citations)} "
                            "verified, substantiated citation(s)"
                        ),
                    ),
                    trace_id=trace_id,
                    claims=verified,
                    citations=citations,
                    citation_integrity=stage11_report.model_dump() if stage11_report else None,
                    semantic_verification=stage12_report.model_dump() if stage12_report else None,
                    publication_gate=gate_result.model_dump() if gate_result else None,
                    publication_status=pub_status,
                    verifier_model=gate_result.verifier_model if gate_result else None,
                ),
                items=items,
                outcome=outcome,
            ),
        )
