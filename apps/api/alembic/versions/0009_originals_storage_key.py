"""document_versions.storage_key + backfill of inline originals (T-V1-039)

Revision ID: 0009_originals_storage_key
Revises: 0008_connector_sync_progress
Create Date: 2026-10-13

Stage 1 of 2 — the *additive* half of "original bytes live in object storage,
not Postgres" (PD-005, T-V1-039). It is deliberately separate from the drop
(``0010_drop_original_bytes``) so it can land on a live database without
breaking the code already running there:

1. add ``document_versions.storage_key`` (nullable — nothing has a key yet);
2. **backfill**: every row still holding bytes is written through
   ``storage.originals`` — the S3/SeaweedFS adapter seam, never a vendor SDK
   from here — and gets its key. The write is checksum-gated twice: bytes that
   do not hash to the row's own ``checksum`` are refused before the put, and
   the object is read back and re-hashed after it. Either refusal raises, and
   because Alembic runs this in one transaction (``Will assume transactional
   DDL``) the column disappears with it — a half-migrated table is not
   possible.

Nothing leaves Postgres at this stage: ``content`` still holds the bytes until
0010, so the pre-ticket worker and API keep working against a 0009 database
(dual copy), and a deploy can be staged as *apply 0009 → deploy → apply 0010*.

Reversible by construction: ``alembic downgrade -1`` drops the pointer column.
It is only reachable after 0010's downgrade has put the bytes back in
``content``, so dropping the pointer cannot lose an original.

Requires the object store: the S3 env compose already gives the API
(``S3_ENDPOINT_URL``/``S3_ACCESS_KEY``/``S3_SECRET_KEY``/``S3_BUCKET``). A
host-side ``make migrate`` must export the same four (runbook in
``handoffs/CODE1-HANDOFF.md``); with no endpoint configured this refuses to
guess rather than talking to the wrong store — see ``storage.originals``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from storage.originals import put_original

revision: str = "0009_originals_storage_key"
down_revision: str | None = "0008_connector_sync_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Concurrent object-store round-trips while backfilling (localhost S3, so the
#: limit is about memory + connection churn, not bandwidth).
_PARALLELISM = 8

#: Rows still carrying inline bytes. ``source_filename`` rides along only to
#: keep the key shape identical to what a fresh upload writes today.
_INLINE_BYTES = """
SELECT dv.id, dv.document_id, d.workspace_id, d.source_filename,
       dv.checksum, dv.content
  FROM document_versions AS dv
  JOIN documents AS d ON d.id = dv.document_id
 WHERE dv.content IS NOT NULL
 ORDER BY dv.id
"""


async def _store_all(rows: Sequence[Any]) -> list[str]:
    """Put every row's bytes in the object store; return the keys in order."""
    gate = asyncio.Semaphore(_PARALLELISM)

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


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column("storage_key", sa.String(length=512), nullable=True),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.text(_INLINE_BYTES)).mappings().all()
    if not rows:
        print("T-V1-039 0009: no rows with inline bytes — nothing to backfill")
        return
    keys = asyncio.run(_store_all(rows))
    for row, key in zip(rows, keys, strict=True):
        bind.execute(
            sa.text("UPDATE document_versions SET storage_key = :key WHERE id = :id"),
            {"key": key, "id": row["id"]},
        )
    print(f"T-V1-039 0009: {len(rows)} original(s) stored in object storage")


def downgrade() -> None:
    # Safe by construction: alembic reaches this revision only after 0010's
    # downgrade has restored every original into `content`, so the pointer can
    # be dropped with the bytes safely back in Postgres.
    op.drop_column("document_versions", "storage_key")
