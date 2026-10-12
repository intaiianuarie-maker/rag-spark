"""Graph wiring factory — V2 work package G4 (in-seam, additive).

This is the *only* place the optional graph leg is constructed from settings.
It lives inside ``packages/graph`` (Stream B's seam) so the protected
``answer.factory.create_orchestrator()`` can adopt it as a one-line change once
v1 Q9 is green and the coordinator dispatches (see
``docs/graph/G4-INTEGRATION-CONTRACT.md``). Until then nothing in
``packages/answer`` or ``packages/core`` imports this module — the flag-off path
never touches ``packages/graph`` and stays byte-identical to v1.

Contract (pinned by ``tests/unit/test_graph_factory.py``):

- ``GRAPH_MODE=off`` (the default) → :func:`create_graph_retriever` returns
  ``None``. The orchestrator's ``graph`` seam then no-ops and the flag-off path
  is byte-identical to v1 (guarded by ``test_graph_off_is_byte_identical_v1``).
- ``GRAPH_MODE`` in ``{rule, llm}`` → a :class:`~graph.retriever.GraphRetriever`
  backed by the embedded :class:`~graph.sqlite_store.SQLiteGraphStore` at
  ``GRAPH_DB_PATH``. No new dependency (stdlib ``sqlite3``), no service.
- The *retrieval* seam is independent of the *extraction* mode: ``rule`` vs
  ``llm`` selects the worker-side extractor (G1), not the retriever built here.
  Both retrieval modes read the same persisted graph.
- An unknown ``GRAPH_MODE`` value is a configuration error and raises
  ``ValueError`` rather than silently disabling the feature (fail loud, never
  silent passthrough on a product path).

Nothing here opens a network socket, loads model weights, or touches product
tables. It only constructs in-process objects.
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
    """Return ``GRAPH_DB_PATH`` or the embedded default."""
    source = os.environ if env is None else env
    return source.get("GRAPH_DB_PATH", DEFAULT_GRAPH_DB_PATH)


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
