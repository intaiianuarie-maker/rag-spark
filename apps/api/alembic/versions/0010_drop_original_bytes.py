"""Drop document_versions.content — originals are object-store only (T-V1-039)

Revision ID: 0010_drop_original_bytes
Revises: 0009_originals_storage_key
Create Date: 2026-10-13

Stage 2 of 2, and the only destructive step: the ``bytea`` column goes away,
leaving ``storage_key`` + ``checksum`` as everything Postgres knows about an
original (ticket metric: **byte columns in PG: 0**).

**The dual-copy window is handled, not assumed away.** A row written by the
pre-T-V1-039 upload path *after* 0009's backfill (0009 applied, old code still
running — exactly what a staged rollout looks like) carries bytes that exist
only in Postgres. This revision moves those to the object store first — same
checksum-gated put as 0009 — and only then drops the column. A re-check after
the move is the safety net: if another old writer committed in between, the
migration raises and rolls back with the column intact rather than destroying
the last copy of those bytes.

Reversible: ``alembic downgrade -1`` re-adds ``content`` and repopulates it
from the object store, byte-identically (``load_original`` re-verifies SHA-256
on the way back in). If any original cannot be restored the downgrade raises
and rolls back rather than leaving a silently empty column — a rollback must
never turn "bytes in object storage" into "bytes nowhere". ``0009``'s downgrade
then drops the pointer, so ``downgrade -2`` reaches the exact pre-ticket schema.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from storage.originals import load_original, put_original

revision: str = "0010_drop_original_bytes"
down_revision: str | None = "0009_originals_storage_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Rows whose bytes exist **only** here: written by the pre-T-V1-039 upload path
#: during the dual-copy window (0009 applied, old code still running). They get
#: moved to the object store below rather than being refused or destroyed.
_STRANDED = """
SELECT dv.id, dv.document_id, d.workspace_id, d.source_filename,
       dv.checksum, dv.content
  FROM document_versions AS dv
  JOIN documents AS d ON d.id = dv.document_id
 WHERE dv.content IS NOT NULL AND dv.storage_key IS NULL
 ORDER BY dv.id
"""

#: Re-check after the move. Can only be non-zero if another pre-T-V1-039 writer
#: committed a row in between — in which case dropping would destroy it, so the
#: migration refuses and rolls back instead (re-run once that writer is gone).
_STILL_STRANDED = """
SELECT count(*)
  FROM document_versions
 WHERE content IS NOT NULL AND storage_key IS NULL
"""

#: Every stored original, for the downgrade's restore pass.
_WITH_KEY = """
SELECT dv.id, dv.storage_key, dv.checksum
  FROM document_versions AS dv
 WHERE dv.storage_key IS NOT NULL
 ORDER BY dv.id
"""


async def _store_all(rows: Sequence[Any]) -> list[str]:
    """Move late inline rows to the object store (checksum-gated put)."""
    gate = asyncio.Semaphore(8)

    async def one(row: Any) -> str:
        async with gate:
            return await put_original(
                workspace_id=row["workspace_id"],
                document_id=row["document_id"],
                version_id=row["id"],
                data=row["content"],
                checksum=row["checksum"],
                filename=row["source_filename"],
            )

    return list(await asyncio.gather(*(one(row) for row in rows)))


async def _restore_all(rows: Sequence[Any]) -> list[bytes]:
    """Read every original back out of the object store (checksum-gated)."""
    gate = asyncio.Semaphore(8)

    async def one(row: Any) -> bytes:
        async with gate:
            return await load_original(row["storage_key"], checksum=row["checksum"])

    return list(await asyncio.gather(*(one(row) for row in rows)))


def upgrade() -> None:
    bind = op.get_bind()
    late = bind.execute(sa.text(_STRANDED)).mappings().all()
    if late:
        # The dual-copy window is a real state, not an error: old code kept
        # writing bytes here after 0009 ran. Move them first — dropping the
        # column while they exist would destroy the only copy.
        keys = asyncio.run(_store_all(late))
        for row, key in zip(late, keys, strict=True):
            bind.execute(
                sa.text("UPDATE document_versions SET storage_key = :key WHERE id = :id"),
                {"key": key, "id": row["id"]},
            )
        print(f"T-V1-039 0010: {len(late)} late inline row(s) moved to object storage")

    stranded = bind.execute(sa.text(_STILL_STRANDED)).scalar_one()
    if stranded:
        raise RuntimeError(
            f"{stranded} document_versions row(s) still carry inline bytes with no "
            "storage_key after the move — another pre-T-V1-039 writer committed while "
            "this migration ran. Dropping the column would destroy those originals; "
            "re-run the migration once that writer is stopped."
        )
    op.drop_column("document_versions", "content")


def downgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column("content", sa.LargeBinary(), nullable=True),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.text(_WITH_KEY)).mappings().all()
    if not rows:
        return
    payloads = asyncio.run(_restore_all(rows))
    for row, data in zip(rows, payloads, strict=True):
        bind.execute(
            sa.text("UPDATE document_versions SET content = :content WHERE id = :id"),
            {"content": data, "id": row["id"]},
        )
    print(f"T-V1-039 0010: {len(rows)} original(s) restored from object storage")
