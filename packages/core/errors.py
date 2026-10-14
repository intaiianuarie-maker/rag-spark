"""Canonical error taxonomy (TASK-Q0, carried end-to-end by T-V1-025).

Single source of truth for failure reason codes, answer/job statuses and
dependency criticality shared by the answer engine, the ingestion worker, the
HTTP API, the shared schemas and the TypeScript contract
(``apps/web/src/app/ask/contracts.ts``):

- every canonical code is a ``UPPER_SNAKE_CASE`` member of :class:`ReasonCode`
  (the case pinned by ``packages/graph/extractor.py``: reason codes follow the
  shared contract without forking the answer taxonomy — there is exactly ONE
  registry, and the graph layer registers no codes of its own);
- every code resolves to an :class:`ErrorSpec` in :data:`ERROR_REGISTRY` with
  a category, an HTTP status, a retryable flag and a **safe** user-facing
  message. "Safe" (AGENTS safe-user-reason, V1 plan Q2): no hostnames, no
  stack traces, no SQL, no exception text — fit to show an end user.
- :class:`AnswerStatus` and :class:`JobStatus` are defined HERE (including the
  additive ``degraded`` / ``partial`` / ``needs_review`` disclosure states) and
  re-exported by ``packages/answer/models.py`` and
  ``packages/ingestion/persistence.py`` so the API, worker and schemas share
  one enum instead of three;
- :data:`DEPENDENCY_MATRIX` classifies every backing dependency as REQUIRED or
  OPTIONAL **per enabled mode** (search / ask / ingest / graph) so a degraded
  dependency can be judged honestly instead of globally.

**Wire policy.** Each registry entry records the *exact* string emitted on the
wire in ``ErrorSpec.wire`` (the canonical ``UPPER_SNAKE_CASE`` value unless an
explicit legacy override is kept).

- **Ingestion + indexing codes moved to the canonical uppercase wire**
  (T-V1-049, mechanical): the raise-time literals in
  ``packages/ingestion/normalize.py``, ``apps/worker/jobs/ingest_pipeline.py``
  and ``apps/worker/jobs/ingest_worker.py``, the value ``normalize_code``
  persists into ``ingestion_jobs.error_code``, and the pinned integration
  assertions in ``tests/integration/test_ingestion_jobs.py`` all moved
  together. ``LEGACY_ALIASES`` still resolves every pre-migration lowercase
  string (``unsupported_mime_type`` → ``UNSUPPORTED_MIME_TYPE``), so old DB
  rows and any late lowercase emitter read back canonical rather than falling
  through as unknown.
- **API envelope codes keep the legacy lowercase wire** (``not_a_member``,
  ``invalid_token``, …): they are pinned by ``apps/web`` (auth-client,
  settings screens) and ~20 integration assertions. Moving them needs a
  versioned contract change plus a coordinated frontend edit — a separate,
  explicit decision, not part of the ingestion migration.
- **Known residue (out of T-V1-049's allowlist):**
  ``apps/api/app/documents/upload.py`` still carries five legacy lowercase
  inline literals (``document_too_large``, ``queue_unavailable``,
  ``invalid_charset``, ``empty_document``, ``job_not_found``). They are
  API-layer literals, not registry-driven, so the upload envelope keeps
  emitting lowercase while the worker/job path for the same code is
  uppercase; the registry documents the governed (worker) wire. A follow-up
  ticket should sweep those five literals and their two pins.

:data:`LEGACY_ALIASES` resolves both the wire value and the lowercase form of
every canonical code back to its member, so old DB rows and new emitters meet
in one place.

Fail-open: :func:`canonical_code` / :func:`normalize_code` /
:func:`error_detail` pass unknown codes through unchanged, so emitters owned
by other streams can never be broken by this registry (and this contract test
can never turn an unknown code into a false green — unknown stays visible).

No third-party code: stdlib ``enum`` + ``dataclasses`` only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class ErrorCategory(StrEnum):
    """Coarse grouping used for dashboards, alerts and routing decisions."""

    ANSWER = "answer"  # answer-quality reasons (never HTTP errors)
    INGESTION = "ingestion"  # parse/chunk/upload pipeline failures
    INDEXING = "indexing"  # FTS/vector index write failures
    STORAGE = "storage"  # backing-store read/write/purge outcomes
    DEPENDENCY = "dependency"  # backing service unreachable/timing out
    API = "api"  # HTTP-layer validation / resource errors
    AUTH = "auth"  # authentication/authorization failures


class ReasonCode(StrEnum):
    """Canonical reason codes (UPPER_SNAKE_CASE) across API/worker/answer.

    Wire values live in :data:`ERROR_REGISTRY` — never compare raw wire
    strings in new code; resolve through :func:`canonical_code`.
    """

    # --- Answer layer (``Answer.reason_code``; engine-emitted) ---
    PARSE_FALLBACK = "PARSE_FALLBACK"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    NO_CLAIMS = "NO_CLAIMS"
    CITATION_INVALID = "CITATION_INVALID"
    PARTIAL_EVIDENCE = "PARTIAL_EVIDENCE"
    DEGRADED_RESULT = "DEGRADED_RESULT"

    # --- Ingestion pipeline / worker job failures ---
    INGESTION_ERROR = "INGESTION_ERROR"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
    EMPTY_DOCUMENT = "EMPTY_DOCUMENT"
    DOCUMENT_TOO_LARGE = "DOCUMENT_TOO_LARGE"
    UNSUPPORTED_MIME_TYPE = "UNSUPPORTED_MIME_TYPE"
    INVALID_CHARSET = "INVALID_CHARSET"
    PARSE_FAILED = "PARSE_FAILED"
    PARSE_ERROR = "PARSE_ERROR"
    CHUNK_FAILED = "CHUNK_FAILED"
    TOO_MANY_ELEMENTS = "TOO_MANY_ELEMENTS"
    QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"
    GRAPH_ENQUEUE_FAILED = "GRAPH_ENQUEUE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NEEDS_REVIEW = "NEEDS_REVIEW"

    # --- Ingestion publication fence (T-V1-019: a job may publish only the
    # generation it was bound to, while that generation is still live) ---
    STALE_GENERATION = "STALE_GENERATION"
    DOCUMENT_DELETED = "DOCUMENT_DELETED"
    GENERATION_GONE = "GENERATION_GONE"

    # --- Indexing (worker → Postgres FTS / vector store) ---
    FTS_INDEXING_FAILED = "FTS_INDEXING_FAILED"
    QDRANT_INDEXING_FAILED = "QDRANT_INDEXING_FAILED"
    INDEX_COUNT_MISMATCH = "INDEX_COUNT_MISMATCH"

    # --- Store lifecycle (verified-store work) ---
    STORE_WRITE_UNVERIFIED = "STORE_WRITE_UNVERIFIED"
    EMBED_UNAVAILABLE = "EMBED_UNAVAILABLE"
    ZERO_VECTOR_REJECTED = "ZERO_VECTOR_REJECTED"
    PARTIAL_PURGE = "PARTIAL_PURGE"
    SUPERSESSION_CLEANUP_FAILED = "SUPERSESSION_CLEANUP_FAILED"
    GRAPH_INDEX_PARTIAL = "GRAPH_INDEX_PARTIAL"

    # --- Backing-dependency availability (per-mode classification) ---
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"
    RETRIEVAL_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"
    RERANK_UNAVAILABLE = "RERANK_UNAVAILABLE"
    GRAPH_UNAVAILABLE = "GRAPH_UNAVAILABLE"
    WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"

    # --- API envelope codes (legacy lowercase pinned on the wire) ---
    FORBIDDEN = "FORBIDDEN"
    INVALID_TOKEN = "INVALID_TOKEN"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    AUTH_UNAVAILABLE = "AUTH_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    EMAIL_TAKEN = "EMAIL_TAKEN"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    KEY_NOT_FOUND = "KEY_NOT_FOUND"
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    ALREADY_MEMBER = "ALREADY_MEMBER"
    NOT_INVITED = "NOT_INVITED"
    NOT_A_MEMBER = "NOT_A_MEMBER"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    LAST_OWNER = "LAST_OWNER"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    COLLECTION_NOT_FOUND = "COLLECTION_NOT_FOUND"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    STORE_PURGE_FAILED = "STORE_PURGE_FAILED"


class AnswerStatus(StrEnum):
    """Terminal status of one answer (shared by API, worker and UI).

    ``degraded`` (T-V1-025): the answer is real and grounded, but one or more
    *optional* capabilities were unavailable on the way (e.g. the reranker) —
    the answer stands, the degradation is disclosed. ``needs_review``: content
    was accepted from a source that was only partially parsed, so the answer
    may be missing regions; the UI must label it rather than render it as a
    clean answer.
    """

    ANSWERED = "answered"
    PARTIAL = "partial"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CLARIFICATION_NEEDED = "clarification_needed"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"
    NEEDS_REVIEW = "needs_review"


class JobStatus(StrEnum):
    """Terminal/interim status of one ingestion job (shared by worker + API).

    ``degraded`` / ``partial`` / ``needs_review`` are additive disclosure
    states (T-V1-025): ``READY`` stays the only "fully published" state, and
    these three describe a job that completed *with* an honest caveat
    (optional dependency down, cleanup partial, or content needing human
    review). Existing DB rows keep their ``queued``/``running``/``ready``/
    ``failed`` values — the column is a plain string, no migration.
    """

    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    DEGRADED = "degraded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class ErrorSpec:
    """Registry entry: how one :class:`ReasonCode` is categorized and emitted.

    ``message`` is the default **safe** user-facing message (no internals).
    ``wire`` is the exact string emitted on the wire (canonical
    UPPER_SNAKE_CASE, or the pinned legacy lowercase value) — see the module
    docstring's wire policy.
    """

    category: ErrorCategory
    http_status: int | None
    retryable: bool
    message: str
    wire: str | None = None

    @property
    def wire_value(self) -> str | None:
        """Exact string emitted on the wire (defaults to the canonical code)."""
        return self.wire


#: Codes disclosed on an otherwise successful result (HTTP 200 / JobStatus
#: READY, or an answer served with a caveat): they are *not* HTTP errors and
#: carry no status of their own.
DISCLOSURE_CODES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.NEEDS_REVIEW,
        ReasonCode.GRAPH_INDEX_PARTIAL,
        ReasonCode.RERANK_UNAVAILABLE,
    }
)

#: Invariant enforced by the taxonomy contract test: an entry has
#: ``http_status=None`` **iff** it is an ANSWER-category quality reason or a
#: disclosure code — every other code is a real HTTP failure.

# registry ------------------------------------------------------------------


def _register(code: ReasonCode, spec: ErrorSpec) -> ErrorSpec:
    """Register a spec, defaulting its wire value to the canonical code."""
    if spec.wire is None:
        spec = replace(spec, wire=code.value)
    ERROR_REGISTRY[code] = spec
    return spec


ERROR_REGISTRY: dict[ReasonCode, ErrorSpec] = {}

# Answer layer: quality reasons, never HTTP errors (http_status=None).
_register(
    ReasonCode.PARSE_FALLBACK,
    ErrorSpec(
        category=ErrorCategory.ANSWER,
        http_status=None,
        retryable=False,
        message="The model response could not be parsed; no claims were verified.",
    ),
)
_register(
    ReasonCode.EVIDENCE_MISSING,
    ErrorSpec(
        category=ErrorCategory.ANSWER,
        http_status=None,
        retryable=False,
        message="Accessible sources do not contain sufficient evidence to answer.",
    ),
)
_register(
    ReasonCode.NO_CLAIMS,
    ErrorSpec(
        category=ErrorCategory.ANSWER,
        http_status=None,
        retryable=False,
        message="The response did not include verifiable claims grounded in citations.",
    ),
)
_register(
    ReasonCode.CITATION_INVALID,
    ErrorSpec(
        category=ErrorCategory.ANSWER,
        http_status=None,
        retryable=False,
        message="Generated claims lacked valid citations in accessible documents.",
    ),
)
_register(
    ReasonCode.PARTIAL_EVIDENCE,
    ErrorSpec(
        category=ErrorCategory.ANSWER,
        http_status=None,
        retryable=False,
        message="Only some claims could be verified with valid citations.",
    ),
)
_register(
    ReasonCode.DEGRADED_RESULT,
    ErrorSpec(
        category=ErrorCategory.ANSWER,
        http_status=None,
        retryable=False,
        message="The answer is grounded, but an optional capability was unavailable.",
    ),
)

# Ingestion pipeline / worker job failures. Wire = the canonical
# UPPER_SNAKE_CASE value the worker writes into ``ingestion_jobs.error_code``
# (T-V1-049: the legacy lowercase literals raised by
# ``apps/worker/jobs/ingest_pipeline.py`` and ``packages/ingestion/normalize.py``
# were uppercased mechanically and the pinned integration assertions moved
# with them; ``LEGACY_ALIASES`` keeps the old lowercase strings resolving).
_register(
    ReasonCode.INGESTION_ERROR,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=400,
        retryable=False,
        message="The document could not be processed.",
    ),
)
_register(
    ReasonCode.JOB_NOT_FOUND,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=404,
        retryable=False,
        message="Ingestion job not found.",
    ),
)
_register(
    ReasonCode.VERSION_NOT_FOUND,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=404,
        retryable=False,
        message="The document version for this job is missing.",
    ),
)
_register(
    ReasonCode.EMPTY_DOCUMENT,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=400,
        retryable=False,
        message="The document is empty.",
    ),
)
_register(
    ReasonCode.DOCUMENT_TOO_LARGE,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=413,
        retryable=False,
        message="File exceeds the maximum allowed size.",
    ),
)
_register(
    ReasonCode.UNSUPPORTED_MIME_TYPE,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=415,
        retryable=False,
        message="The file type is not supported.",
    ),
)
_register(
    ReasonCode.INVALID_CHARSET,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=400,
        retryable=False,
        message="Unknown source charset.",
    ),
)
_register(
    ReasonCode.PARSE_FAILED,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=422,
        retryable=True,
        message="The document could not be parsed.",
    ),
)
_register(
    ReasonCode.PARSE_ERROR,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=422,
        retryable=False,
        message="Parsing failed for this document format.",
    ),
)
_register(
    ReasonCode.CHUNK_FAILED,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=500,
        retryable=True,
        message="Document chunking failed.",
    ),
)
_register(
    ReasonCode.TOO_MANY_ELEMENTS,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=413,
        retryable=False,
        message="The document produced too many elements to process.",
    ),
)
_register(
    ReasonCode.GRAPH_ENQUEUE_FAILED,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=503,
        # Retryable: the graph queue is a broker, so a retry re-attempts the
        # enqueue. T-R10: this code previously existed only as a raised literal
        # in the ingest pipeline and was ABSENT from the taxonomy, which failed
        # the error-taxonomy contract test.
        retryable=True,
        message="Graph extraction could not be queued; the document is not fully indexed yet.",
    ),
)
_register(
    ReasonCode.QUEUE_UNAVAILABLE,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=503,
        retryable=True,
        message="Ingestion queue is unavailable; try again shortly.",
    ),
)
_register(
    ReasonCode.INTERNAL_ERROR,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=500,
        retryable=True,
        message="An unexpected ingestion error occurred.",
    ),
)
_register(
    ReasonCode.NEEDS_REVIEW,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=None,  # disclosure on an otherwise successful result
        retryable=False,
        message="Content was accepted but only partially extracted; review recommended.",
    ),
)

# Ingestion publication fence (T-V1-019). The worker has emitted these three
# as uppercase literals since that ticket (``FENCE_SUPERSEDED`` /
# ``FENCE_DELETED`` / ``FENCE_GONE`` in ``ingest_pipeline.py``); they were not
# registered, so ``normalize_code`` merely passed them through and the
# taxonomy contract never saw them. T-V1-049 registers them: wire stays the
# emitted uppercase value, all three are terminal (never retried — a retry
# would race the same fence again) and carry safe, non-internal messages.
_register(
    ReasonCode.STALE_GENERATION,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=409,  # superseded: the document moved on under the job
        retryable=False,
        message="The document was replaced while this job was running; nothing was published.",
    ),
)
_register(
    ReasonCode.DOCUMENT_DELETED,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=410,  # gone: the document was deleted mid-job
        retryable=False,
        message="The document was deleted before this job could publish; nothing was published.",
    ),
)
_register(
    ReasonCode.GENERATION_GONE,
    ErrorSpec(
        category=ErrorCategory.INGESTION,
        http_status=404,  # the generation row the job was bound to is gone
        retryable=False,
        message="The document generation this job was publishing no longer exists.",
    ),
)

# Indexing: worker → Postgres FTS / vector store.
_register(
    ReasonCode.FTS_INDEXING_FAILED,
    ErrorSpec(
        category=ErrorCategory.INDEXING,
        http_status=503,
        retryable=True,
        message="Full-text indexing failed.",
    ),
)
_register(
    ReasonCode.QDRANT_INDEXING_FAILED,
    ErrorSpec(
        category=ErrorCategory.INDEXING,
        http_status=503,
        retryable=True,
        message="Embedding or vector indexing failed.",
    ),
)
_register(
    ReasonCode.INDEX_COUNT_MISMATCH,
    ErrorSpec(
        category=ErrorCategory.INDEXING,
        http_status=503,
        retryable=True,
        message="Indexed item count does not match the expected count.",
    ),
)

# Store lifecycle (verified-store work).
_register(
    ReasonCode.STORE_WRITE_UNVERIFIED,
    ErrorSpec(
        category=ErrorCategory.STORAGE,
        http_status=503,
        retryable=True,
        message="A store write could not be verified; the operation may not be complete.",
    ),
)
_register(
    ReasonCode.EMBED_UNAVAILABLE,
    ErrorSpec(
        category=ErrorCategory.DEPENDENCY,
        http_status=503,
        retryable=True,
        message="The embedding service is unavailable.",
    ),
)
_register(
    ReasonCode.ZERO_VECTOR_REJECTED,
    ErrorSpec(
        category=ErrorCategory.INDEXING,
        http_status=422,
        retryable=False,
        message="An invalid embedding vector was rejected at the storage boundary.",
    ),
)
_register(
    ReasonCode.PARTIAL_PURGE,
    ErrorSpec(
        category=ErrorCategory.STORAGE,
        http_status=500,
        retryable=False,
        message="The purge completed only partially; some items remain.",
    ),
)
_register(
    ReasonCode.SUPERSESSION_CLEANUP_FAILED,
    ErrorSpec(
        category=ErrorCategory.STORAGE,
        http_status=503,
        retryable=True,
        message="Cleanup of a superseded document version did not complete.",
    ),
)
_register(
    ReasonCode.GRAPH_INDEX_PARTIAL,
    ErrorSpec(
        category=ErrorCategory.STORAGE,
        http_status=None,  # disclosure on an otherwise READY job
        retryable=False,
        message="Graph indexing completed only partially for this document.",
    ),
)

# Backing-dependency availability (classified per mode in DEPENDENCY_MATRIX).
_register(
    ReasonCode.DEPENDENCY_UNAVAILABLE,
    ErrorSpec(
        category=ErrorCategory.DEPENDENCY,
        http_status=503,
        retryable=True,
        message="A required service is temporarily unavailable; try again shortly.",
    ),
)
_register(
    ReasonCode.DEPENDENCY_TIMEOUT,
    ErrorSpec(
        category=ErrorCategory.DEPENDENCY,
        http_status=504,
        retryable=True,
        message="A required service did not respond in time; try again shortly.",
    ),
)
_register(
    ReasonCode.RETRIEVAL_UNAVAILABLE,
    ErrorSpec(
        category=ErrorCategory.DEPENDENCY,
        http_status=503,
        retryable=True,
        message="Search retrieval is unavailable right now.",
    ),
)
_register(
    ReasonCode.RERANK_UNAVAILABLE,
    ErrorSpec(
        category=ErrorCategory.DEPENDENCY,
        http_status=None,  # optional leg: disclosed, answer still served
        retryable=True,
        message="The reranker is unavailable; results were not re-ranked.",
    ),
)
_register(
    ReasonCode.GRAPH_UNAVAILABLE,
    ErrorSpec(
        category=ErrorCategory.DEPENDENCY,
        http_status=503,
        retryable=True,
        message="The graph store is unavailable right now.",
    ),
)
_register(
    ReasonCode.WORKER_UNAVAILABLE,
    ErrorSpec(
        category=ErrorCategory.DEPENDENCY,
        http_status=503,
        retryable=True,
        message="The ingestion worker is unavailable; jobs cannot be processed.",
    ),
)

# API envelope codes. Wire stays the legacy lowercase value: pinned by
# apps/web (auth-client.ts, settings screens) and/or integration tests.
_register(
    ReasonCode.FORBIDDEN,
    ErrorSpec(
        category=ErrorCategory.AUTH,
        http_status=403,
        retryable=False,
        message="You do not have permission to perform this action.",
        wire="forbidden",
    ),
)
_register(
    ReasonCode.INVALID_TOKEN,
    ErrorSpec(
        category=ErrorCategory.AUTH,
        http_status=401,
        retryable=False,
        message="Invalid or expired credentials.",
        wire="invalid_token",
    ),
)
_register(
    ReasonCode.TOKEN_EXPIRED,
    ErrorSpec(
        category=ErrorCategory.AUTH,
        http_status=401,
        retryable=False,
        message="Your session has expired; please sign in again.",
        wire="token_expired",
    ),
)
_register(
    ReasonCode.AUTH_UNAVAILABLE,
    ErrorSpec(
        category=ErrorCategory.AUTH,
        http_status=503,
        retryable=True,
        message="Authentication temporarily unavailable.",
        wire="auth_unavailable",
    ),
)
_register(
    ReasonCode.RATE_LIMITED,
    ErrorSpec(
        category=ErrorCategory.AUTH,
        http_status=429,
        retryable=False,
        message="Too many requests; please try again later.",
        wire="rate_limited",
    ),
)
_register(
    ReasonCode.EMAIL_TAKEN,
    ErrorSpec(
        category=ErrorCategory.AUTH,
        http_status=409,
        retryable=False,
        message="That email address is already registered.",
        wire="email_taken",
    ),
)
_register(
    ReasonCode.VALIDATION_ERROR,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=400,
        retryable=False,
        message="The request payload is invalid.",
        wire="validation_error",
    ),
)
_register(
    ReasonCode.KEY_NOT_FOUND,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=404,
        retryable=False,
        message="API key not found.",
        wire="key_not_found",
    ),
)
_register(
    ReasonCode.MEMBER_NOT_FOUND,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=404,
        retryable=False,
        message="Member no longer exists.",
        wire="member_not_found",
    ),
)
_register(
    ReasonCode.ALREADY_MEMBER,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=409,
        retryable=False,
        message="That user is already a member of this workspace.",
        wire="already_member",
    ),
)
_register(
    ReasonCode.NOT_INVITED,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=403,
        retryable=False,
        message="You are not a member of this workspace.",
        wire="not_invited",
    ),
)
_register(
    ReasonCode.NOT_A_MEMBER,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=403,
        retryable=False,
        message="You do not have access to this workspace.",
        wire="not_a_member",
    ),
)
_register(
    ReasonCode.USER_NOT_FOUND,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=404,
        retryable=False,
        message="No account exists for that email.",
        wire="user_not_found",
    ),
)
_register(
    ReasonCode.LAST_OWNER,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=409,
        retryable=False,
        message="A workspace must keep at least one owner.",
        wire="last_owner",
    ),
)
_register(
    ReasonCode.WORKSPACE_NOT_FOUND,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=404,
        retryable=False,
        message="Workspace not found.",
        wire="workspace_not_found",
    ),
)
_register(
    ReasonCode.COLLECTION_NOT_FOUND,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=404,
        retryable=False,
        message="Collection not found.",
        wire="collection_not_found",
    ),
)
_register(
    ReasonCode.DOCUMENT_NOT_FOUND,
    ErrorSpec(
        category=ErrorCategory.API,
        http_status=404,
        retryable=False,
        message="Document not found.",
        wire="document_not_found",
    ),
)
_register(
    ReasonCode.LLM_UNAVAILABLE,
    ErrorSpec(
        category=ErrorCategory.DEPENDENCY,
        http_status=503,
        retryable=True,
        message="The language model is unavailable; please try again.",
        wire="llm_unavailable",
    ),
)
_register(
    ReasonCode.STORE_PURGE_FAILED,
    ErrorSpec(
        category=ErrorCategory.STORAGE,
        http_status=503,
        retryable=True,
        message="Stored copies could not be fully removed.",
        wire="store_purge_failed",
    ),
)

#: Answer-layer reason codes in emission order — mirrored by the additive
#: ``AnswerReasonCode`` union in ``apps/web/src/app/ask/contracts.ts``
#: (locked by tests/contracts/test_error_taxonomy_contract.py).
ANSWER_REASON_CODES: tuple[ReasonCode, ...] = (
    ReasonCode.PARSE_FALLBACK,
    ReasonCode.EVIDENCE_MISSING,
    ReasonCode.NO_CLAIMS,
    ReasonCode.CITATION_INVALID,
    ReasonCode.PARTIAL_EVIDENCE,
    ReasonCode.DEGRADED_RESULT,
)

#: Which reason codes may accompany each non-answered :class:`AnswerStatus`.
#: ``answered`` carries no reason code (``Answer.reason_code`` is ``None``).
ANSWER_STATUS_REASON_CODES: dict[AnswerStatus, tuple[ReasonCode, ...]] = {
    AnswerStatus.ANSWERED: (),
    AnswerStatus.PARTIAL: (ReasonCode.PARTIAL_EVIDENCE,),
    AnswerStatus.INSUFFICIENT_EVIDENCE: (
        ReasonCode.PARSE_FALLBACK,
        ReasonCode.EVIDENCE_MISSING,
        ReasonCode.NO_CLAIMS,
        ReasonCode.CITATION_INVALID,
    ),
    AnswerStatus.CLARIFICATION_NEEDED: (),
    AnswerStatus.UNAVAILABLE: (
        ReasonCode.DEPENDENCY_UNAVAILABLE,
        ReasonCode.DEPENDENCY_TIMEOUT,
        ReasonCode.RETRIEVAL_UNAVAILABLE,
        ReasonCode.LLM_UNAVAILABLE,
        ReasonCode.EMBED_UNAVAILABLE,
    ),
    AnswerStatus.DEGRADED: (ReasonCode.DEGRADED_RESULT, ReasonCode.RERANK_UNAVAILABLE),
    AnswerStatus.NEEDS_REVIEW: (ReasonCode.NEEDS_REVIEW,),
}

#: Which reason codes may accompany each non-running :class:`JobStatus`.
JOB_STATUS_REASON_CODES: dict[JobStatus, tuple[ReasonCode, ...]] = {
    JobStatus.QUEUED: (ReasonCode.QUEUE_UNAVAILABLE,),
    JobStatus.RUNNING: (),
    JobStatus.READY: (ReasonCode.GRAPH_INDEX_PARTIAL,),
    JobStatus.FAILED: tuple(
        code
        for code in ReasonCode
        if code not in DISCLOSURE_CODES and ERROR_REGISTRY[code].http_status is not None
    ),
    JobStatus.DEGRADED: (
        ReasonCode.DEPENDENCY_UNAVAILABLE,
        ReasonCode.EMBED_UNAVAILABLE,
        ReasonCode.RERANK_UNAVAILABLE,
        ReasonCode.GRAPH_UNAVAILABLE,
    ),
    JobStatus.PARTIAL: (
        ReasonCode.PARTIAL_PURGE,
        ReasonCode.SUPERSESSION_CLEANUP_FAILED,
        ReasonCode.INDEX_COUNT_MISMATCH,
        ReasonCode.STORE_WRITE_UNVERIFIED,
    ),
    JobStatus.NEEDS_REVIEW: (ReasonCode.NEEDS_REVIEW,),
}


# Dependency classification (required vs optional per enabled mode) ---------


class ServiceMode(StrEnum):
    """Enabled capability modes a dependency can be critical to.

    ``search`` (retrieval + rank), ``ask`` (retrieval + generation),
    ``ingest`` (parse/embed/index) and ``graph`` (graph extraction/store,
    enabled by ``GRAPH_MODE``).
    """

    SEARCH = "search"
    ASK = "ask"
    INGEST = "ingest"
    GRAPH = "graph"


class Dependency(StrEnum):
    """Backing services a mode may depend on (probe names match ``/ready``)."""

    POSTGRES = "postgres"
    REDIS = "redis"
    QDRANT = "qdrant"
    MINIO = "minio"
    INFINITY = "infinity"  # embeddings + rerank (PD-002, one service)
    LLM_GATEWAY = "llm_gateway"
    GRAPH_STORE = "graph_store"
    WORKER = "worker"


class DependencyRequirement(StrEnum):
    """Criticality of one dependency in one mode."""

    REQUIRED = "required"  # mode fails closed without it
    OPTIONAL = "optional"  # mode degrades honestly without it
    UNUSED = "unused"  # mode never touches it


@dataclass(frozen=True)
class DependencySpec:
    """How critical one :class:`Dependency` is across the enabled modes."""

    name: Dependency
    #: Modes that fail closed without this dependency.
    required_modes: frozenset[ServiceMode]
    #: Modes that keep working (with an explicit degradation) without it.
    optional_modes: frozenset[ServiceMode]
    #: Reason code emitted when a REQUIRED use of this dependency is down.
    reason_code: ReasonCode


def _spec(
    name: Dependency,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    reason_code: ReasonCode,
) -> DependencySpec:
    return DependencySpec(
        name=name,
        required_modes=frozenset(ServiceMode(mode) for mode in required),
        optional_modes=frozenset(ServiceMode(mode) for mode in optional),
        reason_code=reason_code,
    )


#: Single required/optional classification per dependency and mode. Grounded
#: in what each component actually touches (``apps/api/app/adapters/readiness``
#: probe set, ``apps/worker/jobs/ingest_pipeline.py``, ``packages/retrieval``,
#: ``packages/answer``).
DEPENDENCY_MATRIX: dict[Dependency, DependencySpec] = {
    # Metadata + FTS + job rows: every mode reads or writes Postgres.
    Dependency.POSTGRES: _spec(
        Dependency.POSTGRES,
        required=("search", "ask", "ingest", "graph"),
        optional=(),
        reason_code=ReasonCode.DEPENDENCY_UNAVAILABLE,
    ),
    # Broker + retry scheduling: ingest requires it; answer/search use it as
    # a best-effort cache only.
    Dependency.REDIS: _spec(
        Dependency.REDIS,
        required=("ingest",),
        optional=("search", "ask", "graph"),
        reason_code=ReasonCode.QUEUE_UNAVAILABLE,
    ),
    # Dense vector store: retrieval and ingestion must read/write it.
    Dependency.QDRANT: _spec(
        Dependency.QDRANT,
        required=("search", "ask", "ingest"),
        optional=("graph",),
        reason_code=ReasonCode.RETRIEVAL_UNAVAILABLE,
    ),
    # Original document bytes (PD-005): required to ingest, never read on
    # the answer path.
    Dependency.MINIO: _spec(
        Dependency.MINIO,
        required=("ingest",),
        optional=(),
        reason_code=ReasonCode.DEPENDENCY_UNAVAILABLE,
    ),
    # infinity serves embeddings (mandatory for ingest + query vectorization)
    # and the reranker (optional leg that degrades, never blocks). One probe
    # name, so the required classification wins and the optional rerank leg
    # reports RERANK_UNAVAILABLE at the leg level.
    Dependency.INFINITY: _spec(
        Dependency.INFINITY,
        required=("search", "ask", "ingest"),
        optional=("graph",),
        reason_code=ReasonCode.EMBED_UNAVAILABLE,
    ),
    # Completion gateway: only Ask generates. It IS probed on /ready now:
    # T-V1-027 landed the probe (``llm_gateway`` in readiness.py's
    # ``BASE_DEPENDENCIES`` + probe set), so the old "explicitly not probed"
    # note is obsolete. Still classified here so a probe that passes but a
    # later generation call fails closed with a safe reason instead of a
    # silent empty answer.
    Dependency.LLM_GATEWAY: _spec(
        Dependency.LLM_GATEWAY,
        required=("ask",),
        optional=(),
        reason_code=ReasonCode.LLM_UNAVAILABLE,
    ),
    # Graph store: only critical when the graph mode is enabled.
    Dependency.GRAPH_STORE: _spec(
        Dependency.GRAPH_STORE,
        required=("graph",),
        optional=("search", "ask", "ingest"),
        reason_code=ReasonCode.GRAPH_UNAVAILABLE,
    ),
    # Worker: ingestion jobs cannot progress without it; answers do not.
    Dependency.WORKER: _spec(
        Dependency.WORKER,
        required=("ingest",),
        optional=("search", "ask", "graph"),
        reason_code=ReasonCode.WORKER_UNAVAILABLE,
    ),
}


def classify_dependency(name: Dependency | str, mode: ServiceMode | str) -> DependencyRequirement:
    """Criticality of ``name`` when ``mode`` is enabled.

    Returns ``UNUSED`` for a dependency the mode never touches, so callers can
    skip probing it entirely instead of paging on an irrelevant outage.
    """
    try:
        dep = Dependency(name)
    except ValueError:
        return DependencyRequirement.UNUSED
    try:
        enabled = ServiceMode(mode)
    except ValueError:
        return DependencyRequirement.UNUSED
    spec = DEPENDENCY_MATRIX[dep]
    if enabled in spec.required_modes:
        return DependencyRequirement.REQUIRED
    if enabled in spec.optional_modes:
        return DependencyRequirement.OPTIONAL
    return DependencyRequirement.UNUSED


def required_dependencies(mode: ServiceMode | str) -> frozenset[Dependency]:
    """Every dependency the given enabled mode fails closed without."""
    return frozenset(
        dep
        for dep, spec in DEPENDENCY_MATRIX.items()
        if classify_dependency(dep, mode) is DependencyRequirement.REQUIRED
    )


# resolution helpers --------------------------------------------------------


def spec_for(code: ReasonCode | str) -> ErrorSpec | None:
    """Registry entry for a canonical code or a known legacy wire string."""
    if isinstance(code, ReasonCode):
        return ERROR_REGISTRY.get(code)
    resolved = canonical_code(code)
    return ERROR_REGISTRY.get(resolved) if resolved is not None else None


def canonical_code(raw: str) -> ReasonCode | None:
    """Resolve any known raw string (canonical or legacy) to its code."""
    try:
        return ReasonCode(raw)
    except ValueError:
        pass
    return LEGACY_ALIASES.get(raw)


def normalize_code(raw: str | None) -> str | None:
    """Map a raw code to its wire value; unknown codes pass through unchanged.

    Use this at persistence/serialization boundaries (e.g. ``job.error_code``)
    so legacy rows written before the taxonomy and canonical values resolve to
    one stable wire contract. Identity for every code whose wire value is
    already what the emitter prints — normalization never invents movement.
    """
    if raw is None:
        return None
    resolved = canonical_code(raw)
    if resolved is None:
        return raw
    spec = ERROR_REGISTRY[resolved]
    return spec.wire_value


def error_detail(code: ReasonCode | str, message: str | None = None) -> dict[str, str]:
    """Canonical API error envelope ``{"error", "message"}`` for HTTPException.

    ``error`` is the registry wire value for a known code; unknown codes pass
    through unchanged. ``message`` defaults to the registry's safe message —
    never include hostnames, stack traces, SQL or exception text.
    """
    spec = spec_for(code)
    if spec is None or spec.wire_value is None:
        return {"error": str(code), "message": message or ""}
    return {"error": spec.wire_value, "message": message or spec.message}


#: Wire string → canonical code. Contains every registry wire value plus the
#: lowercase form of every canonical code (so both raise-time literals and
#: canonical values resolve to one member).
LEGACY_ALIASES: dict[str, ReasonCode] = {
    alias: code
    for code, spec in ERROR_REGISTRY.items()
    for alias in (spec.wire, code.value.lower())
    if alias is not None
}
