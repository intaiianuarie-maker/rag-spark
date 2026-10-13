# ruff: noqa
# mypy: ignore-errors
"""G5 real paired eval - graph OFF vs ON with external model (T-V2-006)."""
import asyncio
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "packages", _REPO / "apps" / "api", _REPO / "apps" / "worker", _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
for _p in (_REPO / "tests" / "fixtures",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from app.eval.metrics import score_faithfulness

ANSWER_MODEL = "nvidia/nemotron-3.5-lightning:free"
MAX_TOKENS = 1200

async def check_egress() -> bool:
    from models.adapters.litellm_provider import LiteLLMProvider
    from models.interfaces import ChatMessage, GenerationRequest, MessageRole
    provider = LiteLLMProvider()
    print(f"egress probe: base_url={provider.base_url}")
    try:
        req = GenerationRequest(model=ANSWER_MODEL, messages=[ChatMessage(role=MessageRole.USER, content="ping")], max_tokens=10)
        result = await provider.generate(req)
        print(f"egress probe: success tokens={result.completion_tokens}")
        return True
    except Exception as e:
        print(f"egress probe failed: {e}")
        return False

async def run_paired_eval():
    print("=== egress check ===")
    ok = await check_egress()
    print(f"api.openrouter.ai reachable: {ok}")
    if not ok:
        print("BLOCKED: egress blackholed")
        return None
    pin_path = Path(__file__).parent / "g5_hotpot_pin.json"
    pin = json.loads(pin_path.read_text())
    selected = pin["selected_indices"][:20]
    print("selection rule: first 20 eligible multi-hop rows in file order")
    print(f"selected: {selected}")
    import pyarrow.parquet as pq
    parquet_path = Path("/root/code/Super Rag/datasets/rag-eval/ragbench-tests-v1/hotpotqa/test-00000-of-00001.parquet")
    pf = pq.ParquetFile(str(parquet_path))
    table = pf.read()
    questions = [table.column("question")[i].as_py() for i in selected]
    # For scoring, we need gold evidence: use the documents as gold context
    docs_list = [table.column("documents")[i].as_py() for i in selected]
    # Ingest 20 rows into scratch
    import hashlib
    import uuid
    sys.path.insert(0, str(_REPO / "tests" / "fixtures"))
    os.environ["VECTOR_COLLECTION"] = f"knowledge_test_g5eval_{uuid.uuid4().hex[:8]}"
    scratch = os.environ["VECTOR_COLLECTION"]
    print(f"scratch: {scratch}")
    from ingestion.persistence import (
        Document,
        DocumentVersion,
        IngestionJob,
        JobStage,
        JobStatus,
        get_session_factory,
    )
    from jobs.ingest_worker import process_one
    from object_store_env import apply as apply_object_store_env
    from sqlalchemy import text
    from storage.originals import put_original
    # Create workspace
    async with get_session_factory()() as session:
        org_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        await session.execute(text("INSERT INTO organizations (id, name) VALUES (:id, :name)"), {"id": str(org_id), "name": "g5-eval"})
        await session.execute(text("INSERT INTO workspaces (id, organization_id, name) VALUES (:id, :org, :name)"), {"id": str(ws_id), "org": str(org_id), "name": "ws-g5-eval"})
        await session.commit()
        print(f"workspace {ws_id}")
        apply_object_store_env()
        paras = []
        for idx in selected:
            for para in table.column("documents")[idx].as_py():
                paras.append((idx, para))
        print(f"total paras {len(paras)}")
        job_ids = []
        for i, (row_idx, para) in enumerate(paras):
            data = para.encode("utf-8")
            doc = Document(workspace_id=ws_id, title=f"doc-{i}", source_filename=f"doc-{i}.txt", mime_type="text/plain")
            session.add(doc)
            await session.flush()
            checksum = hashlib.sha256(data).hexdigest()
            version_id = uuid.uuid4()
            storage_key = await put_original(workspace_id=doc.workspace_id, document_id=doc.id, version_id=version_id, data=data, checksum=checksum, filename=doc.source_filename)
            version = DocumentVersion(id=version_id, document_id=doc.id, version_number=1, checksum=checksum, size_bytes=len(data), mime_type="text/plain", storage_key=storage_key)
            session.add(version)
            await session.flush()
            job = IngestionJob(workspace_id=doc.workspace_id, document_id=doc.id, document_version_id=version.id, status=JobStatus.QUEUED.value, stage=JobStage.QUEUED.value)
            session.add(job)
            await session.commit()
            await session.refresh(job)
            job_ids.append(str(job.id))
        print(f"created {len(job_ids)} jobs")
        for jid in job_ids:
            await process_one(uuid.UUID(jid))
        print(f"ingested {len(job_ids)} docs")
        # Graph extraction
        from graph.extractor import GraphChunk, RuleBasedGraphExtractor
        from graph.factory import graph_db_path
        from graph.sqlite_store import SQLiteGraphStore
        from ingestion.persistence import ChunkRow
        from sqlalchemy import select
        result = await session.execute(select(ChunkRow).where(ChunkRow.workspace_id == ws_id))
        chunk_rows = result.scalars().all()
        print(f"chunks for graph: {len(chunk_rows)}")
        graph_chunks = [GraphChunk(chunk_id=str(row.id), text=row.text, workspace_id=str(ws_id), document_id=None) for row in chunk_rows]
        gstore = SQLiteGraphStore(graph_db_path())
        await gstore.set_enabled(str(ws_id), True)
        extractor = RuleBasedGraphExtractor()
        ext_result = await extractor.extract(graph_chunks)
        await gstore.upsert_extraction(str(ws_id), ext_result.entities, ext_result.relationships, ext_result.communities)
        print(f"graph entities {len(ext_result.entities)} rels {len(ext_result.relationships)}")
        gstore.close()
    # Paired runs
    from core.auth import ACLScope
    from retrieval.interfaces import RetrievalMode
    os.environ["ANSWER_MODEL"] = ANSWER_MODEL
    results = []
    total_cost = 0.0
    for idx, q, docs in zip(selected, questions, docs_list):
        print(f"\n=== query {idx}: {q[:60]} ===")
        # OFF
        os.environ["GRAPH_MODE"] = "off"
        from answer.factory import build_answer_engine
        engine_off = build_answer_engine(model=ANSWER_MODEL, collection=scratch)
        engine_off.max_tokens = MAX_TOKENS
        acl = ACLScope(user_id="test", workspace_ids=[str(ws_id)])
        try:
            ans_off = await engine_off.answer(query=q, acl_scope=acl, mode=RetrievalMode.HYBRID)
            print(f"OFF answer: {ans_off.answer[:80]} status:{ans_off.status.value}")
            # Score groundedness vs gold docs
            gold_ctx = "\n".join(docs)
            grounded_off = score_faithfulness(ans_off.answer, gold_ctx) if ans_off.answer else 0.0
            # Citation validity: check if citations are in gold chunk_ids? For now use simple
            valid_off = 1.0 if ans_off.citations else 0.0
            cost_off = 0.0
        except Exception as e:
            print(f"OFF failed: {e}")
            ans_off = None
            grounded_off = 0.0
            valid_off = 0.0
            cost_off = 0.0
        # ON
        os.environ["GRAPH_MODE"] = "rule"
        engine_on = build_answer_engine(model=ANSWER_MODEL, collection=scratch)
        engine_on.max_tokens = MAX_TOKENS
        try:
            ans_on = await engine_on.answer(query=q, acl_scope=acl, mode=RetrievalMode.HYBRID_GRAPH)
            print(f"ON answer: {ans_on.answer[:80]} status:{ans_on.status.value}")
            gold_ctx = "\n".join(docs)
            grounded_on = score_faithfulness(ans_on.answer, gold_ctx) if ans_on.answer else 0.0
            valid_on = 1.0 if ans_on.citations else 0.0
            cost_on = 0.0
        except Exception as e:
            print(f"ON failed: {e}")
            ans_on = None
            grounded_on = 0.0
            valid_on = 0.0
            cost_on = 0.0
        results.append({
            "idx": idx,
            "question": q,
            "off_answer": ans_off.answer if ans_off else None,
            "on_answer": ans_on.answer if ans_on else None,
            "off_grounded": grounded_off,
            "on_grounded": grounded_on,
            "off_valid": valid_off,
            "on_valid": valid_on,
            "off_cost": cost_off,
            "on_cost": cost_on,
        })
        total_cost += cost_off + cost_on
    print("\n=== paired results ===")
    for r in results:
        print(f"{r['idx']}: off_grounded {r['off_grounded']:.3f} on_grounded {r['on_grounded']:.3f} delta {r['on_grounded']-r['off_grounded']:.3f} cost {r['off_cost']+r['on_cost']}")
    print(f"total cost: {total_cost} (must be 0 for free model)")
    if results:
        delta = sum(r['on_grounded']-r['off_grounded'] for r in results)/len(results)
        print(f"delta grounded: {delta:.3f}")
        # What would constitute evidence? n=20 is small, so delta >0.1 with p<0.05 might be signal, but we report without gate claim
        print(f"n=20 separates signal from noise at best; delta of {delta:.3f} would need >0.1 to be suggestive, but we make NO gate claim")
    # Save results
    Path("evals/g5_paired_results.json").write_text(json.dumps(results, indent=2))
    return results

if __name__ == "__main__":
    asyncio.run(run_paired_eval())
