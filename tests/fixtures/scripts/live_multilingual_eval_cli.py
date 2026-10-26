"""Live M7 storage/language checks and independent M8 production evaluator CLI.

Run in the release API environment (same DB/vector/provider configuration),
never the local shared host. The private manifest contains public fixture
answers and UI-created dataset/run identities, not credentials. This performs
fresh retrieval: no fake retriever, supplied traces, or replay_snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for directory in (REPO / "packages", REPO / "apps/api"):
    sys.path.insert(0, str(directory))


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


async def run(manifest: dict, report: dict) -> dict:
    from app.auth.db import get_session_factory
    from app.eval.models import EvaluationDataset
    from app.eval.router import RetrievalConfig, _make_retriever, create_orchestrator
    from app.eval.runner import evaluate_dataset
    from app.eval.storage import EvaluationStore
    from core.auth import ACLScope
    from ingestion.metadata import detect_language
    from ingestion.persistence import ChunkRow, Document, DocumentVersion
    from models.router_config import load_workspace_config
    from sqlalchemy import select

    workspace = uuid.UUID(manifest["workspace_id"])
    # Only owned run IDs and document UUIDs cross this seam. SQL cannot inspect
    # another workspace or an unowned document, even with a malformed manifest.
    async with get_session_factory()() as session:
        for upload in manifest["uploads"]:
            identity = uuid.UUID(upload["document_id"])
            document = (await session.execute(select(Document).where(
                Document.id == identity, Document.workspace_id == workspace,
                Document.deleted_at.is_(None),
            ))).scalar_one_or_none()
            require(document is not None, "owned_document_missing")
            chunks = (await session.execute(select(ChunkRow).join(
                DocumentVersion, ChunkRow.document_version_id == DocumentVersion.id,
            ).where(
                DocumentVersion.document_id == identity,
                DocumentVersion.id == document.current_version_id,
                ChunkRow.workspace_id == workspace,
            ))).scalars().all()
            languages = sorted({chunk.language or "" for chunk in chunks})
            passed = document.language == upload["language"] and bool(chunks) and all(
                chunk.language == upload["language"] for chunk in chunks
            )
            report["storage"].append({"document_id": str(identity),
                "document_language": document.language, "chunk_count": len(chunks),
                "chunk_languages": languages, "passed": passed})
            if not passed:
                report["failures"].append("stored_language_mismatch")

        for answer in manifest["answers"]:
            language = detect_language(answer["answer"])
            passed = language == answer["language"]
            report["answers"].append({"case_id": answer["case_id"],
                "detected_language": language, "expected_language": answer["language"],
                "passed": passed})
            if not passed:
                report["failures"].append("answer_language_mismatch")

        store = EvaluationStore(session)
        config = RetrievalConfig.model_validate(manifest["retrieval"])
        policy = load_workspace_config(str(workspace))
        sensitive = policy.sensitive if policy else False
        for requested in manifest["evaluations"]:
            try:
                dataset = await store.get_dataset(workspace, requested["dataset_id"])
                saved = await store.get_run(workspace, requested["run_id"])
                require(dataset is not None and saved is not None, "ui_evaluation_missing")
                # The DB-owned saved dataset must exactly match what the real UI
                # created. Never score caller-provided IDs against fake outcomes.
                expected = EvaluationDataset.model_validate(requested["dataset"])
                require(dataset == expected, "ui_dataset_mismatch")
                require(saved.dataset_id == dataset.id, "ui_run_dataset_mismatch")
                require(saved.config_snapshot["retrieval"] == config.model_dump(),
                        "ui_retrieval_configuration_mismatch")
                require(saved.config_snapshot["accounting"] == "retrieval_usage_unreported",
                        "ui_run_not_fresh_retrieval")
                orchestrator = create_orchestrator(sensitive=sensitive)
                orchestrator.rerank_enabled = config.reranker
                fresh = await evaluate_dataset(dataset, _make_retriever(
                    orchestrator, ACLScope(workspace_ids=[str(workspace)]), config,
                ), config_snapshot={"retrieval": config.model_dump(), "sensitive": sensitive,
                    "experiment": {}, "accounting": "retrieval_usage_unreported"})
                metric_keys = {
                    "recall", "precision", "mrr", "ndcg", "citation_accuracy", "faithfulness",
                }
                require(set(fresh.results) == set(saved.results) == metric_keys,
                        "metric_schema_mismatch")
                same = all(math.isfinite(fresh.results[key]) and math.isclose(
                    fresh.results[key], saved.results[key], rel_tol=0, abs_tol=1e-12,
                ) for key in metric_keys)
                same = same and fresh.config_snapshot["dataset_sha256"] == saved.config_snapshot[
                    "dataset_sha256"]
                same = same and (fresh.config_snapshot["scorer"] == saved.config_snapshot["scorer"])
                same = same and fresh.tokens == saved.tokens and fresh.cost == saved.cost and (
                    fresh.accounting_complete == saved.accounting_complete)
                retrieval_passed = fresh.results["recall"] == saved.results["recall"] == 1.0
                report["evaluations"].append({"gate": requested["gate"],
                    "ui_run_id": saved.id, "cli_run_id": fresh.id, "dataset_id": dataset.id,
                    "dataset_sha256": fresh.config_snapshot["dataset_sha256"],
                    "scorer": fresh.config_snapshot["scorer"], "metrics_ui": saved.results,
                    "metrics_cli": fresh.results, "metrics_equal": same,
                    "retrieval_passed": retrieval_passed,
                    "latency_ms_ui": saved.latency_ms, "latency_ms_cli": fresh.latency_ms,
                    "latency_comparison": "independent_wall_clock_measurements",
                    "accounting_complete": fresh.accounting_complete})
                if not same:
                    report["failures"].append("ui_cli_metrics_differ")
                if not retrieval_passed:
                    report["failures"].append("expected_sources_not_all_retrieved")
            except Exception as error:
                report["evaluations"].append({"gate": requested["gate"],
                    "metrics_equal": False, "error_type": type(error).__name__})
                report["failures"].append("live_evaluation_failed")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result: dict = {"storage": [], "answers": [], "evaluations": [], "failures": []}
    try:
        require(args.manifest.stat().st_mode & 0o077 == 0, "manifest_must_be_private")
        # Browser/Node processes intentionally receive no provider key. Only
        # this backend CLI may read the private key file, before any production
        # provider/config module is imported. Its value never enters argv,
        # the Node environment, the manifest, or the outcome report.
        if not os.environ.get("OPENROUTER_API_KEY"):
            key_file = os.environ.get("LIVE_EVAL_PROVIDER_KEY_FILE")
            if key_file:
                key_path = Path(key_file)
                require(key_path.is_file() and key_path.stat().st_mode & 0o077 == 0,
                        "provider_key_file_must_be_private")
                payload = json.loads(key_path.read_text(encoding="utf8"))
                key = payload.get("OPENROUTER_API_KEY")
                require(isinstance(key, str) and bool(key.strip()), "provider_key_missing")
                os.environ["OPENROUTER_API_KEY"] = key
        asyncio.run(run(json.loads(args.manifest.read_text(encoding="utf8")), result))
    except Exception as error:
        # Adapter errors may contain URLs, credentials, or source content.
        result["failure"] = {"stage": "live_cli", "error_type": type(error).__name__}
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return 1 if result.get("failure") or result.get("failures") else 0


if __name__ == "__main__":
    raise SystemExit(main())
