"""Factory seams for the V2 graph leg (G2).

Wires the durable shared graph store (single source of truth for the DB path)
and the optional graph retriever behind the same ``GRAPH_MODE`` / ``GRAPH_DB_PATH``
settings so api and worker see the same rows.

This factory performs no network and no heavier imports at call time beyond
``packages.graph`` itself — retrieval is built lazily only when the leg is ON.
"""

from __future__ import annotations

import os
from pathlib import Path

from .retriever import GraphRetriever
from .sqlite_store import SQLiteGraphStore

#: Default on-disk location for the embedded SQLiteGraphStore (G2). Mirrors the
#: ``GRAPH_DB_PATH`` default documented in ``.env.example``.
DEFAULT_GRAPH_DB_PATH = "./.graph/graph.db"

#: ``GRAPH_MODE`` values that enable the graph leg. ``off`` is the v1 default.
#: ``rule`` / ``llm`` select the extraction strategy (G1) but share one
#: retrieval path, so they are equivalent for :func:`create_graph_retriever`.
GRAPH_MODES_ON = frozenset({"rule", "llm"})
GRAPH_MODE_OFF = "off"


def graph_mode(env: dict[str, str] | None = None) -> str:
    """Return the normalized ``GRAPH_MODE`` (lowercased, default ``off``)."""
    source = os.environ if env is None else env
    return source.get("GRAPH_MODE", GRAPH_MODE_OFF).strip().lower()


def graph_db_path(env: dict[str, str] | None = None) -> str:
    """Return ``GRAPH_DB_PATH`` or the embedded default.

    T-R11: a RELATIVE default is a trap in a container. Each service resolves
    `"./.graph/graph.db"`` inside its own filesystem, so api and worker silently
    end up with two private graph databases and neither can see the other's
    rows. The compose file now pins an absolute path on a shared volume for
    both; this default stays relative only for local single-process use.

    The parent directory is created on demand: on a fresh named volume the
    directory does not exist yet, and SQLite fails to open a file whose parent
    is missing — which would look like a permissions fault rather than a
    configuration one.
    """
    source = os.environ if env is None else env
    path = source.get("GRAPH_DB_PATH", DEFAULT_GRAPH_DB_PATH)
    parent = Path(path).expanduser().parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    return path


def create_graph_retriever(
    *,
    db_path: str | Path | None = None,
    mode: str | None = None,
    env: dict[str, str] | None = None,
) -> GraphRetriever | None:
    """Build the optional graph retriever from settings, or ``None`` when off.

    Parameters mirror what the protected factory will pass so the seam is a
    drop-in: ``answer.factory.create_orchestrator()`` will call
    ``create_graph_retriever()`` and hand the result to
    ``HybridRetrievalOrchestrator(..., graph=<result>)``.

    - ``mode`` overrides ``GRAPH_MODE``; ``db_path`` overrides ``GRAPH_DB_PATH``.
    - ``env`` is an injectable mapping (tests pass a dict instead of mutating
      ``os.environ``); it defaults to the process environment.
    - Returns ``None`` (never a stub) when the mode is ``off``.
    - Raises ``ValueError`` for a mode that is neither ``off`` nor in
      :data:`GRAPH_MODES_ON` so a typo fails loudly instead of silently
      disabling retrieval.
    """
    resolved_mode = mode.strip().lower() if mode is not None else graph_mode(env)
    if resolved_mode == GRAPH_MODE_OFF:
        return None
    if resolved_mode not in GRAPH_MODES_ON:
        raise ValueError(
            f"invalid GRAPH_MODE {resolved_mode!r}; expected one of "
            f"{sorted(GRAPH_MODES_ON | {GRAPH_MODE_OFF})}"
        )
    resolved_path = db_path if db_path is not None else graph_db_path(env)
    store = SQLiteGraphStore(resolved_path)
    return GraphRetriever(store)


def create_graph_store(
    *,
    db_path: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> SQLiteGraphStore:
    """Build the durable graph store connected to the shared SQLite database.

    This is the single factory the worker job (and any other write path) must use
    to obtain a store so the shared graph database is wired cleanly — never a
    hard-coded path or an ad-hoc ``SQLiteGraphStore(...)`` in call sites.

    T-R11: a relative path is a trap in containers. The compose file pins an
    absolute ``GRAPH_DB_PATH`` on a shared volume for api + worker; this factory
    honours it (or the embedded default for local single-process use). The
    parent directory is created on demand by :func:`graph_db_path`.

    Parameters mirror :func:`create_graph_retriever` so callers switching between
    a read seam (retriever) and a write seam (store) pass identical arguments.
    """
    resolved_path = db_path if db_path is not None else graph_db_path(env)
    return SQLiteGraphStore(resolved_path)
