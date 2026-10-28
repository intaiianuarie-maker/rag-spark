"""LlamaIndex-backed chunker (rag-fork-solution TASK-M2-002).

Reuse, per `rag-fork-solution/DECISIONS.md` (Chunking → LlamaIndex, MIT):
``llama_index.core.node_parser.SentenceSplitter`` performs the
sentence-boundary-aware packing. Everything product-specific stays ours:

* **Heading-aware grouping** comes from our own sectioning of the normalized
  ``DocumentElement`` stream (``chunk.sections_from_elements``) — LlamaIndex's
  MarkdownNodeParser would require a markdown round-trip that loses page and
  section fidelity, which citations depend on.
* **Deterministic token counting** is injected into the splitter
  (``tokenizer=whitespace_tokens``) instead of LlamaIndex's default tiktoken
  tokenizer, and **deterministic sentence segmentation** is injected
  (``chunking_tokenizer_fn=split_sentences``) instead of the default nltk
  ``punkt`` model. The pinned wheel happens to bundle both model caches, but
  relying on that is fragile: a missing cache makes LlamaIndex fall through to
  ``nltk.download()`` / a tiktoken fetch at first use (i.e. a network call
  inside the worker), and any model upgrade would change sentence boundaries
  and therefore chunk counts. Injecting both keeps counts reproducible and
  keeps the worker offline-safe.
* **Parent-child structure, Chunk model, page/section/element_types/metadata**
  are assembled by our code (``chunk.make_chunk``), so the adapter is a drop-in
  for the built-in chunker and the persisted chunk rows are unchanged.

No LlamaIndex type crosses this boundary — the adapter returns our ``Chunk`` /
``ChunkResult`` (``packages/ingestion/interfaces.py`` model).

License: LlamaIndex MIT (see rag-fork-solution/REUSE_MAP.md).
"""

from __future__ import annotations

import re
from typing import Any

from ..chunk import (
    DEFAULT_MAX_TOKENS,
    Chunk,
    ChunkResult,
    make_chunk,
    sections_from_elements,
)
from ..interfaces import DocumentElement, ParsedDocument
from ..metadata import DocumentMetadata, extract_metadata

LLAMA_INDEX_CHUNKER_VERSION = "0.14.25"


class LlamaIndexUnavailableError(RuntimeError):
    """Raised when the optional ``llama-index-core`` package is missing.

    Callers (the ingest pipeline) fall back to the built-in chunker and log a
    warning; chunking correctness must not depend on an optional dependency
    being present in every image.
    """


def is_available() -> bool:
    """Probe the optional SDK inside its adapter, without loading model weights."""
    try:
        import llama_index.core  # noqa: F401 - preserves the actual importability probe
    except ImportError:
        return False
    return True


def whitespace_tokens(text: str) -> list[str]:
    """Deterministic token list (whitespace words) for the LlamaIndex splitter.

    LlamaIndex measures chunk sizes with ``len(tokenizer(text))``; returning the
    word list keeps its accounting identical to our documented approximation
    (``chunk.count_tokens``) with no tiktoken model download at runtime.
    """
    return text.split()


# Sentence end = . ! ? or the full-width equivalents (U+3002, U+FF01, U+FF1F).
# The pattern is built from escaped codepoints so this source file stays ASCII.
# Each sentence keeps its own trailing whitespace; unterminated trailing text is
# still emitted (the last match runs to the end of the string), so no words are
# ever dropped.
_TERMINATORS = ".!?\u3002\uff01\uff1f"
_SENTENCE_RE = re.compile(f"[^{_TERMINATORS}]+[{_TERMINATORS}]*\\s*")


def split_sentences(text: str) -> list[str]:
    """Deterministic sentence segmentation (replaces LlamaIndex's nltk punkt).

    Handles the Latin (v1 languages: ro/pt/en) and full-width CJK terminators
    so a CJK document cannot collapse into a single oversized "sentence" that
    then bypasses sentence-aware splitting.
    """
    return [match.group(0) for match in _SENTENCE_RE.finditer(text) if match.group(0).strip()]


def build_sentence_splitter(max_tokens: int) -> Any:
    """SentenceSplitter wired to our deterministic tokenizer.

    Raises :class:`LlamaIndexUnavailableError` when llama-index-core is not
    installed (the pipeline then falls back to the built-in chunker).
    """
    try:
        from llama_index.core.node_parser import SentenceSplitter
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise LlamaIndexUnavailableError(
            "llama-index-core is not installed; install "
            f"llama-index-core=={LLAMA_INDEX_CHUNKER_VERSION}"
        ) from exc

    return SentenceSplitter(
        chunk_size=max_tokens,
        # Overlap is intentionally 0: our parent/continuation structure carries
        # the context, and overlap would make chunk boundaries non-deterministic
        # across sentence boundaries.
        chunk_overlap=0,
        separator=" ",
        tokenizer=whitespace_tokens,
        chunking_tokenizer_fn=split_sentences,
    )


# Internal alias kept for readability at the call site.
_build_splitter = build_sentence_splitter


def chunk_elements(
    elements: list[DocumentElement],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    language: str = "unknown",
    document_metadata: DocumentMetadata | None = None,
    splitter: Any | None = None,
) -> list[Chunk]:
    """Chunk a normalized element stream with LlamaIndex sentence splitting.

    ``splitter`` exists for tests; in production the splitter is built from
    ``max_tokens`` so the cap and the split sizes always agree.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    splitter = splitter if splitter is not None else _build_splitter(max_tokens)

    chunks: list[Chunk] = []
    for group in sections_from_elements(elements):
        combined = " ".join(text for text in group["texts"] if text).strip()
        if not combined:
            continue
        # SentenceSplitter keeps sentences intact while respecting the cap that
        # our tokenizer measures.
        pieces = [piece.strip() for piece in splitter.split_text(combined) if piece.strip()]
        if not pieces:
            continue
        if len(pieces) <= 1:
            chunks.append(
                make_chunk(
                    len(chunks),
                    pieces[0],
                    parent_id=None,
                    group=group,
                    language=language,
                    document_metadata=document_metadata,
                )
            )
            continue

        parent_id = f"chunk-{len(chunks):04d}"
        chunks.append(
            make_chunk(
                len(chunks),
                pieces[0],
                parent_id=None,
                group=group,
                language=language,
                document_metadata=document_metadata,
            )
        )
        for piece in pieces[1:]:
            chunks.append(
                make_chunk(
                    len(chunks),
                    piece,
                    parent_id=parent_id,
                    group=group,
                    language=language,
                    document_metadata=document_metadata,
                )
            )
    return chunks


def chunk_document(
    parsed: ParsedDocument,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    filename: str | None = None,
    splitter: Any | None = None,
) -> ChunkResult:
    """Chunk a parsed document (LlamaIndex splitter) + attach metadata/language."""
    metadata = extract_metadata(parsed, filename=filename)
    chunks = chunk_elements(
        parsed.elements,
        max_tokens=max_tokens,
        language=metadata.language,
        document_metadata=metadata,
        splitter=splitter,
    )
    return ChunkResult(chunks=chunks, language=metadata.language, metadata=metadata)


def backend_name() -> str:
    """Identifier recorded in job/stage logs (and the RAG debugger)."""
    return f"llamaindex-sentence-splitter@{LLAMA_INDEX_CHUNKER_VERSION}"
