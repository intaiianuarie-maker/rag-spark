"""G4 factory seam tests — graph retriever construction from settings.

Pins the in-seam contract documented in ``packages/graph/factory.py`` and the
G4 integration contract (``docs/graph/G4-INTEGRATION-CONTRACT.md``):

- ``GRAPH_MODE=off`` → ``None`` (the flag-off, byte-identical-to-v1 path).
- ``GRAPH_MODE`` in ``{rule, llm}`` → a ``GraphRetriever`` over a
  ``SQLiteGraphStore`` at ``GRAPH_DB_PATH`` (no network, no weights, no product
  tables).
- Unknown mode → ``ValueError`` (fail loud, no silent disable on a product path).
- The retrieval seam is independent of extraction mode: ``rule`` and ``llm``
  build the same retriever shape.

These are hermetic unit tests: the SQLite store opens a temp file under
``tmp_path`` (never the real ``./.graph/graph.db``). No external services.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from graph import GraphRetriever
from graph.factory import (
    DEFAULT_GRAPH_DB_PATH,
    GRAPH_MODES_ON,
    create_graph_retriever,
    graph_db_path,
    graph_mode,
)
from graph.sqlite_store import SQLiteGraphStore

pytestmark = pytest.mark.unit


def test_graph_mode_off_returns_none() -> None:
    """Flag-off (the v1 default) must build no graph object at all."""
    retriever = create_graph_retriever(mode="off", db_path="/unused/graph.db")
    assert retriever is None


def test_graph_mode_defaults_to_off_via_env() -> None:
    """No GRAPH_MODE set → treated as off (byte-identical v1 path)."""
    assert graph_mode({}) == "off"
    assert create_graph_retriever(env={}) is None


@pytest.mark.parametrize("mode", sorted(GRAPH_MODES_ON))
def test_graph_mode_on_builds_sqlite_backed_retriever(
    mode: str, tmp_path: Path
) -> None:
    """rule/llm both build a GraphRetriever over a SQLiteGraphStore."""
    db = tmp_path / "graph.db"
    retriever = create_graph_retriever(mode=mode, db_path=db)
    assert isinstance(retriever, GraphRetriever)
    # Seam check: the retriever is backed by the embedded SQLiteGraphStore.
    assert isinstance(retriever._store, SQLiteGraphStore)
    # The store is lazily connected; touching it opens the temp file only.
    assert retriever._store._path == Path(db)


def test_graph_mode_is_case_and_space_insensitive() -> None:
    assert graph_mode({"GRAPH_MODE": "  RULE "}) == "rule"
    assert create_graph_retriever(mode="  Off ", db_path="/unused") is None


def test_graph_db_path_default_and_override() -> None:
    assert graph_db_path({}) == DEFAULT_GRAPH_DB_PATH
    assert graph_db_path({"GRAPH_DB_PATH": "/custom/g.db"}) == "/custom/g.db"


def test_invalid_graph_mode_raises() -> None:
    """A typo must fail loudly, never silently disable the feature."""
    with pytest.raises(ValueError, match="invalid GRAPH_MODE"):
        create_graph_retriever(mode="grph", db_path="/unused/graph.db")


def test_env_mapping_is_read_when_no_explicit_mode(tmp_path: Path) -> None:
    """An injected env mapping drives construction without touching os.environ."""
    db = tmp_path / "from-env.db"
    retriever = create_graph_retriever(env={"GRAPH_MODE": "rule", "GRAPH_DB_PATH": str(db)})
    assert isinstance(retriever, GraphRetriever)
