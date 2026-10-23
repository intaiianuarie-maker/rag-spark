"""T-SMOKE-FORMATS: Multi-format ingestion and retrieval evaluation smoke test.

Ingests 5 distinct file types:
1. PDF (digital PDF with pages and structure)
2. DOCX (Word document with tables)
3. Markdown (structured document with headings)
4. Plain Text (legal notice / procedural text)
5. HTML (semantic HTML with headings and paragraphs)

Parses, chunks, indexes into vector & lexical stores, and retrieves with
citable provenance.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "packages", _REPO / "apps/api", _REPO / "tests/fixtures", _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.auth import ACLScope  # noqa: E402
from harness_fakes import (  # noqa: E402
    FakeLexicalStore,
    FakeVectorStore,
    ReferenceHybridOrchestrator,
    make_record,
)
from ingestion.adapters.registry import parse_with_precedence  # noqa: E402
from ingestion.chunk import chunk_document  # noqa: E402

FORMAT_FIXTURES: list[dict[str, Any]] = [
    {
        "format": "pdf",
        "mime_type": "application/pdf",
        "filename": "document.pdf",
        "path": "tests/fixtures/formats/pdf_digital.pdf",
        "query": "digital document heading",
    },
    {
        "format": "docx",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "filename": "table_document.docx",
        "path": "tests/fixtures/formats/docx_tables.docx",
        "query": "table column cell data",
    },
    {
        "format": "markdown",
        "mime_type": "text/markdown",
        "filename": "guide.md",
        "data": (
            b"# Knowledge Architecture\n\n"
            b"Antigravity Knowledge OS integrates hybrid vector search with BM25 lexical ranking.\n"
            b"\n## Verification Gate\n\n"
            b"Stage 11 deterministic citation validation ensures zero fabricated references."
        ),
        "query": "Stage 11 deterministic citation validation",
    },
    {
        "format": "txt",
        "mime_type": "text/plain",
        "filename": "policy.txt",
        "data": (
            b"TERMS OF SERVICE AND STATUTORY NOTICE\n"
            b"Under statutory regulation Article 42, the mandatory termination notice period\n"
            b"is exactly 30 business days prior to contract cessation."
        ),
        "query": "mandatory termination notice period 30 business days",
    },
    {
        "format": "html",
        "mime_type": "text/html",
        "filename": "portal.html",
        "data": (
            b"<!DOCTYPE html><html><head><title>System Operations</title></head>"
            b"<body><h1>Production Emergency Procedures</h1>"
            b"<p>In the event of an automated failover, verify database replication health</p>"
            b"</body></html>"
        ),
        "query": "Production Emergency Procedures automated failover",
    },
]


async def run_smoke_formats_battery() -> dict[str, Any]:
    """Execute end-to-end ingest + retrieve verification across 5 file formats."""
    results: dict[str, Any] = {
        "formats_tested": 0,
        "formats_successful": 0,
        "details": {},
    }

    dim = 8
    vector_store = FakeVectorStore(dimension=dim)
    await vector_store.ensure_collection(collection="smoke_formats", dimension=dim)
    lexical_store = FakeLexicalStore()

    orchestrator = ReferenceHybridOrchestrator(
        dense=vector_store,
        lexical=lexical_store,
        collection="smoke_formats",
    )

    workspace_id = "ws-smoke"
    all_records = []

    for fixture in FORMAT_FIXTURES:
        fmt = fixture["format"]
        mime = fixture["mime_type"]
        filename = fixture["filename"]

        if "path" in fixture:
            data = await asyncio.to_thread(Path(fixture["path"]).read_bytes)
        else:
            data = fixture["data"]

        # 1. Parse with precedence
        parsed_doc, parse_report = await parse_with_precedence(
            data,
            mime_type=mime,
            filename=filename,
        )

        assert len(parsed_doc.elements) > 0, f"No elements parsed for {fmt}"

        # 2. Chunk document
        chunk_res = chunk_document(parsed_doc, filename=filename)
        assert len(chunk_res.chunks) > 0, f"No chunks generated for {fmt}"

        # 3. Create records for storage
        doc_id = f"doc-{fmt}"
        for chunk in chunk_res.chunks:
            # Deterministic test vector
            vec = [0.1 * ((i + len(fmt)) % 10) for i in range(dim)]
            rec = make_record(
                record_id=f"{doc_id}-{chunk.id}",
                workspace_id=workspace_id,
                content=chunk.text,
                vector=vec,
                document_id=doc_id,
                page=chunk.page or 1,
                section=chunk.section or "main",
            )
            all_records.append(rec)

        results["details"][fmt] = {
            "mime_type": mime,
            "parser_used": parse_report.used,
            "elements_count": len(parsed_doc.elements),
            "chunks_count": len(chunk_res.chunks),
            "language": chunk_res.language,
        }

    # Index all chunks
    await vector_store.upsert(collection="smoke_formats", records=all_records)
    await lexical_store.index(records=all_records)

    # 4. Retrieve and verify each format
    scope = ACLScope(user_id="user-smoke", workspace_ids=[workspace_id])

    for fixture in FORMAT_FIXTURES:
        fmt = fixture["format"]
        query = fixture["query"]
        doc_id = f"doc-{fmt}"

        hits = await orchestrator.retrieve(
            query=query,
            acl_scope=scope,
            dense_top_k=5,
            fusion_top_k=5,
        )

        # Confirm at least one hit corresponds to this format's document
        matching = [h for h in hits if h.document_id == doc_id]
        results["details"][fmt]["retrieval_hits"] = len(hits)
        results["details"][fmt]["retrieval_matched"] = len(matching) > 0

        assert len(matching) > 0, f"Failed to retrieve ingested content for {fmt} (query: {query})"
        results["formats_successful"] += 1

    results["formats_tested"] = len(FORMAT_FIXTURES)
    return results


def main() -> None:
    results = asyncio.run(run_smoke_formats_battery())
    out_path = Path(__file__).resolve().parent / "smoke_formats_results.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    success = results["formats_successful"]
    total = results["formats_tested"]
    print(f"Smoke battery passed: {success}/{total} formats successful.")
    print(f"Results recorded at: {out_path}")


if __name__ == "__main__":
    main()
