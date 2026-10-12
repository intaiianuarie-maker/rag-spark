"""Reindex all database chunks into Postgres FTS (lexical_docs) and Qdrant."""

import argparse
import asyncio
import logging
import math

from ingestion.persistence import get_session_factory
from retrieval.adapters.infinity_embed import InfinityEmbeddingProvider
from retrieval.adapters.pg_fts import PostgresLexicalStore
from retrieval.adapters.qdrant_store import QdrantVectorStore
from retrieval.interfaces import VectorRecord
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reindex")


def validate_vector(vector: list[float], expected_dim: int) -> None:
    """Validate vector length, finiteness, and ensure it is not all zeros."""
    if len(vector) != expected_dim:
        raise ValueError(f"Vector dimension mismatch: got {len(vector)}, expected {expected_dim}")
    if any(not math.isfinite(x) for x in vector):
        raise ValueError("Vector contains non-finite values (NaN or Inf)")
    if all(x == 0.0 for x in vector):
        raise ValueError("Manufactured all-zero vector rejected")


async def reindex(
    *,
    dry_run: bool = False,
    batch_size: int = 100,
    collection: str = "knowledge",
) -> int:
    session_factory = get_session_factory()
    lexical_store = PostgresLexicalStore(session_factory=session_factory)
    embed_provider = InfinityEmbeddingProvider()
    dim = embed_provider.dimension

    logger.info(
        "Reindex configuration: model=%s, dimension=%d, collection=%s, dry_run=%s",
        embed_provider.model_name,
        dim,
        collection,
        dry_run,
    )

    qdrant = QdrantVectorStore(collection=collection)
    if not dry_run:
        await qdrant.ensure_collection(collection=collection, dimension=dim)

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

    logger.info("Found %d live chunks to index.", len(rows))
    if not rows:
        await qdrant.close()
        return 0

    total_indexed = 0
    for i in range(0, len(rows), batch_size):
        batch_rows = rows[i : i + batch_size]
        texts = [r.text for r in batch_rows]

        logger.info("Generating embeddings for batch %d-%d...", i, i + len(batch_rows))
        vectors = await embed_provider.embed_documents(texts)
        if len(vectors) != len(batch_rows):
            raise RuntimeError(
                f"Embedding count mismatch: expected {len(batch_rows)}, got {len(vectors)}"
            )

        batch_records: list[VectorRecord] = []
        for r, vec in zip(batch_rows, vectors, strict=True):
            validate_vector(vec, dim)
            batch_records.append(
                VectorRecord(
                    id=str(r.id),
                    vector=vec,
                    workspace_id=str(r.workspace_id),
                    document_id=str(r.document_id),
                    document_version_id=str(r.document_version_id),
                    chunk_id=str(r.id),
                    content=r.text,
                    page=r.page,
                    section=r.section,
                    metadata={"language": r.language or "en", **(r.chunk_metadata or {})},
                )
            )

        if dry_run:
            logger.info(
                "[DRY RUN] Would index batch %d to %d (%d records)",
                i,
                i + len(batch_records),
                len(batch_records),
            )
        else:
            await lexical_store.index(records=batch_records)
            await qdrant.upsert(collection=collection, records=batch_records)
            logger.info("Indexed batch %d to %d", i, i + len(batch_records))

        total_indexed += len(batch_records)

    await qdrant.close()
    logger.info("Reindexing complete! Total chunks indexed: %d", total_indexed)
    return total_indexed


async def main() -> None:
    parser = argparse.ArgumentParser(description="Reindex all chunks into FTS and Qdrant")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate and embed without writing to stores"
    )
    parser.add_argument(
        "--batch-size", type=int, default=100, help="Batch size for embedding and indexing"
    )
    parser.add_argument(
        "--collection", type=str, default="knowledge", help="Qdrant collection name"
    )
    args = parser.parse_args()

    await reindex(dry_run=args.dry_run, batch_size=args.batch_size, collection=args.collection)


if __name__ == "__main__":
    asyncio.run(main())
