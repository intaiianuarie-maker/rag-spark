"""T-V1-071: point a test run at a scratch vector collection, never the live one.

T-V1-069 proved the integration suites were persisting vectors into the **live**
``knowledge`` collection (stubbed embedder + hardcoded name). The product seam is
now ``core.config.vector_collection()`` (``VECTOR_COLLECTION``, default
``knowledge``); this helper is its test-side driver:

* :func:`use_scratch_collection` exports ``VECTOR_COLLECTION`` **before any app
  module is imported** — pydantic settings cache on first read, so the export has
  to happen at module import time, which is why every suite calls it at the top
  of its own module rather than in a fixture;
* :func:`assert_not_live` is the guard a suite can use to prove it never names
  the live collection;
* :func:`live_points_count` / :func:`collection_points_count` are the read-only
  count-stability proof for the ticket metric (the live count must not move while
  a suite runs).

Nothing here writes: counts are ``GET /collections/<name>`` reads only.
"""

from __future__ import annotations

import os
import urllib.request
import uuid

QDRANT = os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")

#: The production collection that must never be written by a test run.
LIVE_COLLECTION = "knowledge"


def use_scratch_collection(tag: str) -> str:
    """Export ``VECTOR_COLLECTION`` to a namespaced scratch name; return it.

    Call at module import time, before importing ``app.*``/``jobs.*``. A random
    suffix means two suites (or two runs) never share an index, so a leftover
    point can never make another suite's assertions pass or fail by accident.
    """
    name = f"knowledge_test_{tag}_{uuid.uuid4().hex[:8]}"
    os.environ["VECTOR_COLLECTION"] = name
    return name


def effective_collection() -> str:
    """What the product code would use right now (settings, not the env var)."""
    return os.environ.get("VECTOR_COLLECTION") or "knowledge"


def assert_not_live(name: str | None = None) -> str:
    """Fail loudly if this run is pointed at the live collection."""
    target = name or effective_collection()
    if target == LIVE_COLLECTION:
        raise AssertionError(
            f"test run is pointed at the LIVE {LIVE_COLLECTION!r} collection — "
            "a suite must call use_scratch_collection() before importing app code"
        )
    return target


def _points_count(name: str) -> int | None:
    try:
        with urllib.request.urlopen(f"{QDRANT}/collections/{name}", timeout=15) as r:
            import json

            return int(json.loads(r.read())["result"]["points_count"])
    except Exception:
        return None


def live_points_count() -> int | None:
    """Read-only count of the live collection (the ticket's stability metric)."""
    return _points_count(LIVE_COLLECTION)


def collection_points_count(name: str) -> int | None:
    return _points_count(name)