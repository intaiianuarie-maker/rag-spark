#!/usr/bin/env python3
"""T-V1-021 crash-worker fixture: run the REAL ingest worker loop with a
stage-targeted stall so the parent test can SIGKILL this process mid-stage.

This is not a mock worker: it imports ``jobs.ingest_worker.run_worker`` — the
same loop the compose worker container runs — and drives the same claim →
process → ack cycle against the same live Redis/Postgres/Qdrant. The only
injection is one *stall seam* chosen by ``T021_CRASH_STAGE``:

- ``after_claim``  — stall inside ``process_one`` before any pipeline work
                     (the claim is held; the job row is still ``queued``);
- ``parse``        — stall inside the parser's ``parse`` (job row
                     ``running``/``parsing``; no durable writes yet);
- ``index``        — stall inside the Postgres FTS ``index`` before its
                     commit (chunk rows durable, FTS rows not);
- ``embed``        — stall inside ``embed_documents`` before the Qdrant
                     upsert (chunks + FTS durable, vectors not);
- ``publication``  — stall at the pre-READY publication gate after every
                     verified store write (everything durable except READY).

When the stall is reached the process writes ``<stage>.stalled`` into
``T021_MARKER_DIR`` and then waits forever, keeping its claim lease alive via
the worker's own heartbeat — exactly like a wedged-but-alive worker. The
parent test waits for the marker and SIGKILLs: the heartbeat dies with the
process, the lease expires (``INGEST_QUEUE_LEASE_SECONDS``, set to 2 by the
test — env only), and a restarted worker must redeliver and reconcile.

With ``T021_CRASH_STAGE`` unset this script is the *recovery* worker: the same
loop with no stall, used to reconcile after the crash.

Embeddings note (same rationale as tests/integration/test_dedup_versions.py):
this box's Infinity serves 384-d where the ``knowledge`` collection expects
1024-d (T-V1-043, open), which would gate the vector stage with
EMBED_UNAVAILABLE before any point is written. A 1024-d non-zero stub provider
satisfies the pipeline's shape check and the store's T-V1-015 vector boundary
while every store write still goes to the REAL Qdrant/Postgres through the
real adapters.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_MARKER_SUFFIX = ".stalled"


def _log(message: str) -> None:
    print(f"[t021-crash-worker pid={os.getpid()} t={time.time():.3f}] {message}", flush=True)


async def _stall(marker_dir: Path, stage: str, detail: str) -> None:
    """Announce the crash point durably, then hang while the lease heartbeat lives."""
    marker = marker_dir / f"{stage}{_MARKER_SUFFIX}"
    marker.write_text(f"pid={os.getpid()} stage={stage} detail={detail} t={time.time():.3f}\n")
    _log(f"STALL_REACHED stage={stage} detail={detail} marker={marker}")
    await asyncio.Event().wait()


def _install_embedder(crash_stage: str, marker_dir: Path) -> None:
    from typing import Any

    from retrieval.adapters import infinity_embed

    class _StubProvider:
        """1024-d non-zero fakes (see module docstring, T-V1-043)."""

        dimension = 1024

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[0.001] * self.dimension for _ in texts]

    provider: Any = _StubProvider
    if crash_stage == "embed":

        class _StallingProvider(_StubProvider):
            async def embed_documents(self, texts: list[str]) -> list[list[float]]:
                await _stall(marker_dir, "embed", f"chunks={len(texts)} pre-qdrant-upsert")
                return await super().embed_documents(texts)

        provider = _StallingProvider
    # Deliberate monkeypatch of the adapter's provider class (fixture-only):
    # the pipeline lazy-imports this name at call time, so the stub is picked
    # up without touching product code. Typed as Any to keep mypy quiet.
    infinity_embed.InfinityEmbeddingProvider = provider  # type: ignore[misc]


def _install_stall_seam(crash_stage: str, marker_dir: Path) -> None:
    from typing import Any

    if crash_stage == "embed":
        return  # handled by the embedder provider above
    if crash_stage == "after_claim":
        from jobs import ingest_worker

        original_process_one = ingest_worker.process_one

        async def _stalling_process_one(job_id) -> None:
            await _stall(marker_dir, "after_claim", "claim held, no pipeline work started")
            await original_process_one(job_id)

        ingest_worker.process_one = _stalling_process_one
    elif crash_stage == "parse":
        from jobs import ingest_pipeline

        original_parser_for_mime: Any = ingest_pipeline.parser_for_mime

        class _StallingParser:
            def __init__(self, inner) -> None:
                self._inner = inner

            async def parse(self, *args, **kwargs):
                await _stall(marker_dir, "parse", "parser invoked, no durable writes")
                return await self._inner.parse(*args, **kwargs)

        def _stalling_parser_for_mime(mime_type):
            inner = original_parser_for_mime(mime_type)
            return _StallingParser(inner) if inner is not None else None

        ingest_pipeline.parser_for_mime = _stalling_parser_for_mime
    elif crash_stage == "index":
        from retrieval.adapters import pg_fts

        original_index = pg_fts.PostgresLexicalStore.index

        async def _stalling_index(self, *, records):
            await _stall(
                marker_dir, "index", f"fts write in flight, records={len(records)} pre-commit"
            )
            return await original_index(self, records=records)

        # Fixture-only method patch on the store class the pipeline
        # lazy-imports at call time.
        pg_fts.PostgresLexicalStore.index = _stalling_index  # type: ignore[method-assign]
    elif crash_stage == "publication":
        from jobs import ingest_pipeline

        original_gate = ingest_pipeline.assert_generation_is_publishable

        async def _stalling_gate(session, *, job, document_id, version_id, gate):
            if gate == "ready":
                await _stall(
                    marker_dir,
                    "publication",
                    "all store writes verified, READY not committed",
                )
            return await original_gate(
                session, job=job, document_id=document_id, version_id=version_id, gate=gate
            )

        ingest_pipeline.assert_generation_is_publishable = _stalling_gate
    else:
        raise SystemExit(f"unknown T021_CRASH_STAGE={crash_stage!r}")


def main() -> int:
    crash_stage = os.environ.get("T021_CRASH_STAGE", "").strip()
    marker_dir = Path(os.environ["T021_MARKER_DIR"])
    marker_dir.mkdir(parents=True, exist_ok=True)

    _install_embedder(crash_stage, marker_dir)
    if crash_stage:
        _install_stall_seam(crash_stage, marker_dir)

    from jobs.ingest_worker import run_worker

    _log(f"worker_loop_starting crash_stage={crash_stage or 'none(recovery)'}")
    asyncio.run(run_worker(asyncio.Event()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
