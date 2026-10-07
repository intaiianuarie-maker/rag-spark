"""Unit tests for the LlamaIndex chunker adapter (rag-fork-solution TASK-M2-002).

Acceptance from the card:
  - a parent-child fixture produces linked chunks;
  - chunk count + sizes are deterministic;
  - the token cap, page and section survive the LlamaIndex splitting.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ingestion.adapters.llamaindex_chunker import (
    LlamaIndexUnavailableError,
    backend_name,
    build_sentence_splitter,
    chunk_document,
    chunk_elements,
    split_sentences,
    whitespace_tokens,
)
from ingestion.chunk import ChunkResult, count_tokens
from ingestion.interfaces import DocumentElement, ElementType, ParsedDocument


def _element(
    element_type: ElementType,
    text: str,
    section: str | None = None,
    page: int | None = None,
) -> DocumentElement:
    return DocumentElement(type=element_type, text=text, section=section, page=page)


@pytest.fixture(scope="module")
def splitter():
    """Real SentenceSplitter via the adapter's builder (shared code path)."""
    from ingestion.adapters.llamaindex_chunker import build_sentence_splitter

    return build_sentence_splitter(64)


def test_parent_child_fixture_produces_linked_chunks(splitter) -> None:
    elements = [
        _element(ElementType.TITLE, "Quarterly Report"),
        _element(ElementType.HEADING, "1. Introduction", section="Introduction", page=1),
        _element(ElementType.TEXT, " ".join(["alpha"] * 400), section="Introduction", page=1),
        _element(ElementType.HEADING, "2. Methods", section="Methods", page=2),
        _element(ElementType.TEXT, "short body", section="Methods", page=2),
    ]
    chunks = chunk_elements(elements, max_tokens=64, splitter=splitter)

    assert chunks, "expected chunks"
    ids = {c.id for c in chunks}
    parents = [c for c in chunks if c.parent_id is None]
    children = [c for c in chunks if c.parent_id is not None]
    assert parents and children, "parent-child links must exist for a long section"

    for child in children:
        assert child.parent_id in ids
        assert child.parent_id != child.id

    sections = {c.section for c in chunks}
    assert {"Introduction", "Methods"} <= sections
    assert {c.page for c in chunks} >= {1, 2}


def test_chunk_count_and_sizes_are_deterministic(splitter) -> None:
    elements = [
        _element(ElementType.HEADING, "A", section="A"),
        _element(ElementType.TEXT, " ".join(["beta"] * 300), section="A"),
    ]
    first = chunk_elements(elements, max_tokens=64, splitter=splitter)
    second = chunk_elements(elements, max_tokens=64, splitter=splitter)
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]
    assert [c.index for c in first] == list(range(len(first)))


def test_token_cap_respected_under_llamaindex_splitting() -> None:
    from ingestion.adapters.llamaindex_chunker import build_sentence_splitter

    # The splitter carries its own chunk_size; production builds it from
    # max_tokens (chunk_elements), so match them here.
    cap = 50
    elements = [
        _element(ElementType.HEADING, "Cap", section="Cap"),
        _element(ElementType.TEXT, " ".join(["gamma"] * 500), section="Cap"),
    ]
    chunks = chunk_elements(elements, max_tokens=cap, splitter=build_sentence_splitter(cap))
    assert chunks
    assert all(count_tokens(c.text) <= cap for c in chunks), "token cap must hold"


def test_chunk_document_attaches_language_and_metadata(splitter) -> None:
    parsed = ParsedDocument(
        elements=[
            _element(ElementType.HEADING, "Salut", section="Salut"),
            _element(ElementType.TEXT, "Bun ziua, aceasta este o propozitie.", section="Salut"),
        ],
        language="ro",
        page_count=1,
    )
    result = chunk_document(parsed, filename="salut.txt", splitter=splitter)
    assert isinstance(result, ChunkResult)
    assert result.chunks
    assert result.chunks[0].metadata.get("language") in {"ro", "unknown"}


def test_missing_dependency_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If llama-index-core is absent the adapter raises a typed, catchable error."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name.startswith("llama_index"):
            raise ImportError("No module named 'llama_index'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(LlamaIndexUnavailableError):
        chunk_elements([_element(ElementType.TEXT, "x", section="x")], max_tokens=64)


def test_backend_name_is_pinned() -> None:
    assert backend_name().startswith("llamaindex-sentence-splitter@")


def _pipeline() -> Any:
    """Import the worker pipeline module (needs apps/worker on PYTHONPATH)."""
    from jobs import ingest_pipeline

    return ingest_pipeline


def test_pipeline_default_backend_is_llamaindex() -> None:
    pipeline = _pipeline()
    assert pipeline.CHUNKING_BACKEND == "llamaindex"
    assert pipeline.resolved_chunking_backend().startswith(
        "llamaindex-sentence-splitter@"
    )


def _isolated_pipeline(backend: str | None) -> Any:
    """Load a private copy of the pipeline module with CHUNKING_BACKEND set.

    The module reads the env var at import time, so it must be re-executed to be
    re-configured. Reloading the shared module in place would swap its class
    objects and break other test modules that imported them by reference, so a
    separately-named copy is executed from the same source file instead.
    """
    import importlib.util
    import os
    import sys
    from pathlib import Path

    source = Path(_pipeline().__file__).resolve()
    name = f"_kos_chunking_probe_{backend}"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves types through sys.modules; register before exec.
    sys.modules[name] = module

    saved = {k: os.environ.get(k) for k in ("CHUNKING_BACKEND",)}
    if backend is None:
        os.environ.pop("CHUNKING_BACKEND", None)
    else:
        os.environ["CHUNKING_BACKEND"] = backend
    try:
        spec.loader.exec_module(module)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.modules.pop(name, None)
    return module


def test_pipeline_backend_env_opt_out() -> None:
    """CHUNKING_BACKEND=builtin selects the M2-001b chunker (A/B opt-out)."""
    pipeline = _isolated_pipeline("builtin")
    assert pipeline.CHUNKING_BACKEND == "builtin"
    assert pipeline.resolved_chunking_backend() == "builtin-whitespace"


def test_pipeline_backend_env_is_normalized() -> None:
    assert _isolated_pipeline("  LLAMAINDEX  ").CHUNKING_BACKEND == "llamaindex"


def test_pipeline_falls_back_when_llamaindex_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing optional dep degrades to the built-in chunker, loudly.

    The warning assertion runs through ``caplog``, which needs the logger to be
    enabled. Alembic's ``env.py`` calls ``logging.config.fileConfig`` with the
    default ``disable_existing_loggers=True`` every time migrations run (e.g. an
    earlier integration test), which sets ``disabled=True`` on our ingestion
    logger for the rest of the process — so the ``logger.warning`` becomes a
    no-op and ``caplog.text`` is empty even though the code is correct. That is
    a cross-cutting config concern owned by ``apps/api`` (out of scope here), so
    this test re-enables its own logger under monkeypatch. Do not remove: it is
    what makes the loud-fallback assertion hold in a full-process run.
    """
    import builtins
    import logging

    ingestion_logger = logging.getLogger("knowledge_os.worker.ingest")
    monkeypatch.setattr(ingestion_logger, "disabled", False)
    monkeypatch.setattr(ingestion_logger, "propagate", True)

    pipeline = _isolated_pipeline("llamaindex")
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name.startswith("llama_index"):
            raise ImportError("simulated: llama-index-core not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert pipeline.resolved_chunking_backend() == "builtin-whitespace"
    with caplog.at_level("WARNING"):
        result = pipeline._chunk(
            ParsedDocument(
                elements=[_element(ElementType.TEXT, "o propozitie.", section="S")]
            ),
            filename="t.txt",
        )
    assert isinstance(result, ChunkResult)
    assert result.chunks
    assert "chunking_backend_fallback" in caplog.text


def test_backend_chunk_contracts_are_equivalent() -> None:
    """Both backends emit the identical Chunk contract for the same input.

    Guards the persisted chunk rows and the citation mapping: TASK-M2-002 must
    be a drop-in swap of the M2-001b chunker, not a new chunk shape.
    """
    from ingestion.chunk import chunk_document as builtin_chunk_document

    body = (
        "Raportul prezinta rezultatele trimestriale. Costurile au crescut cu "
        "douasprezece la suta fata de perioada precedenta. "
        " remarks remain mixed-language? Da."
    )
    parsed = ParsedDocument(
        elements=[
            _element(ElementType.HEADING, "Rezumat", section="Rezumat", page=1),
            _element(ElementType.TEXT, body, section="Rezumat", page=1),
        ],
        language="ro",
        page_count=1,
    )
    cap = 24
    built_in = builtin_chunk_document(parsed, filename="raport.md", max_tokens=cap)
    with_li = chunk_document(parsed, filename="raport.md", max_tokens=cap)

    assert set(built_in.chunks[0].model_dump()) == set(with_li.chunks[0].model_dump())
    assert built_in.language == with_li.language
    assert built_in.metadata.model_dump() == with_li.metadata.model_dump()
    for result in (built_in, with_li):
        ids = {c.id for c in result.chunks}
        assert [c.index for c in result.chunks] == list(range(len(result.chunks)))
        assert all(c.token_count <= cap for c in result.chunks)
        assert all(c.parent_id in ids for c in result.chunks if c.parent_id)
        assert {c.page for c in result.chunks} == {1}
        assert {c.section for c in result.chunks} == {"Rezumat"}
        # Heading + body survive exactly once, in order.
        assert " ".join(c.text for c in result.chunks).split() == (
            ["Rezumat", *body.split()]
        )


def test_splitter_injects_our_deterministic_tokenizers(splitter) -> None:
    """Our tokenizer + sentence splitter are injected, not LlamaIndex defaults.

    The defaults resolve through LlamaIndex's model caches (tiktoken encoding,
    nltk punkt): bundled in the pinned wheel, but a missing cache makes LlamaIndex
    fall through to a download at first use, and any model upgrade would shift
    sentence boundaries and therefore chunk counts. Checked through the
    splitter's own attributes so this test imports no vendor SDK.
    """
    assert splitter._tokenizer is whitespace_tokens
    assert splitter._chunking_tokenizer_fn is split_sentences
    assert splitter.chunk_overlap == 0


def test_chunking_never_reads_tokenizer_model_data(
    splitter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Splitting must not resolve tiktoken/nltk model data.

    Any such access raises, so the test fails loudly if a future change drops an
    injected callable and reintroduces model-data resolution in the worker.
    """
    import nltk

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("tokenizer model data accessed at runtime")

    monkeypatch.setattr(nltk, "download", explode)
    monkeypatch.setattr(nltk.data, "find", explode)
    # Point NLTK at an empty dir: if anything resolves punkt, it must fail.
    monkeypatch.setenv("NLTK_DATA", str(tmp_path))

    pieces = splitter.split_text(
        "Bun ziua. Aceasta este o propozitie despre rapoarte! Costuri? Da. " * 5
    )
    assert [p for p in pieces if p.strip()]
    assert not any(tmp_path.iterdir()), "NLTK data dir must stay empty"


def test_split_sentences_is_deterministic_and_lossless() -> None:
    # Terminators: "." "!" "?" plus the full-width U+3002 / U+FF01. The last
    # fragment has none and must still be emitted (no dropped tail).
    text = (
        "Prima propozitie. A doua! A treia? "
        "Text fara terminator. Ok\uff01 Ultima fraza"
    )
    sentences = split_sentences(text)
    assert sentences == split_sentences(text)
    assert len(sentences) == 6
    assert sentences[-1].strip() == "Ultima fraza"
    # No word is dropped or duplicated: whitespace-normalized text survives.
    assert "".join(sentences).split() == text.split()


def test_chunking_never_drops_or_duplicates_text() -> None:
    """Concatenated chunk text reconstructs the section text (word-for-word)."""
    body = (
        "Raportul prezinta rezultatele. Costurile au crescut semnificativ! "
        " remarks remain in English? Da. Ultima fraza nu are terminator"
    )
    cap = 12
    elements = [_element(ElementType.TEXT, body, section="Rezumat")]
    chunks = chunk_elements(
        elements, max_tokens=cap, splitter=build_sentence_splitter(cap)
    )

    assert len(chunks) > 1, "fixture must actually split"
    assert all(count_tokens(c.text) <= cap for c in chunks)
    # parent + children together carry the section exactly once each.
    assert " ".join(c.text for c in chunks).split() == body.split()