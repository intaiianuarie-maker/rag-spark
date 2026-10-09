"""Reindex all database chunks into Postgres FTS (lexical_docs) and Qdrant."""

import asyncio
import logging

from ingestion.persistence import get_session_factory
from retrieval.adapters.pg_fts import PostgresLexicalStore
from retrieval.adapters.qdrant_store import QdrantVectorStore
from retrieval.interfaces import VectorRecord
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reindex")


async def main():
    session_factory = get_session_factory()
    lexical_store = PostgresLexicalStore(session_factory=session_factory)
    qdrant = QdrantVectorStore(collection="knowledge")
    await qdrant.ensure_collection(collection="knowledge", dimension=1024)

    logger.info("Fetching existing chunks from Postgres...")
    async with session_factory() as session:
        query = text(
            """
            SELECT c.id, c.workspace_id, dv.document_id, c.document_version_id,
                   c.text, c.page, c.section, c.language, c.chunk_metadata
            FROM chunks c
            JOIN document_versions dv ON c.document_version_id = dv.id
            JOIN documents d ON dv.document_id = d.id
            WHERE d.deleted_at IS NULL
            """
        )
        rows = (await session.execute(query)).all()

    logger.info(f"Found {len(rows)} live chunks to index.")
    if not rows:
        return

    records = [
        VectorRecord(
            id=str(r.id),
            vector=[0.0] * 1024,
            workspace_id=str(r.workspace_id),
            document_id=str(r.document_id),
            document_version_id=str(r.document_version_id),
            chunk_id=str(r.id),
            content=r.text,
            page=r.page,
            section=r.section,
            metadata={"language": r.language or "en", **(r.chunk_metadata or {})},
        )
        for r in rows
    ]

    # Index into lexical FTS in batches of 100
    batch_size = 100
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        await lexical_store.index(records=batch)
        await qdrant.upsert(collection="knowledge", records=batch)
        logger.info(f"Indexed batch {i} to {i + len(batch)}")

    await qdrant.close()
    logger.info("Reindexing complete!")


if __name__ == "__main__":
    asyncio.run(main())
