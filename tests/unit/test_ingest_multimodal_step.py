"""Opt-in MULTIMODAL_EMBED_ENABLED ingest step (plain sync; asyncio.run)."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters import pdfium_render
from jobs import ingest_pipeline as ip

pytestmark = pytest.mark.unit

WS, DOC, VER, JOB = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


class _Env:
    def __init__(self, monkeypatch, *, pages=3, png=lambda p: f"png-{p}".encode() * 10,
                 embed_fail_page=None, upsert_error=None, sensitive=False, vec_len=3072):
        self.upserts: list = []
        self.deleted: list = []
        self.ensured: list = []
        self.embedded_urls: list[str] = []
        self.render_threads: list[int] = []
        self.closed = 0
        env = self

        monkeypatch.setattr(pdfium_render, "get_pdf_page_count", lambda _c: pages)

        def render(_c, page, dpi=150):
            env.render_threads.append(threading.get_ident())
            return png(page), 612.0, 792.0, 1, 1

        monkeypatch.setattr(pdfium_render, "render_pdf_page", render)

        class Provider:
            model_name = "google/gemini-embedding-2"
            revision = "google/gemini-embedding-2"
            dimension = 3072

            def __init__(self, **_kw):
                pass

            async def embed_multimodal(self, part):
                env.embedded_urls.append(part.image_url["url"])
                if part.page == embed_fail_page:
                    raise RuntimeError("provider boom")
                return [0.5] * vec_len

        class Qdrant:
            def __init__(self, *a, **k):
                pass

            async def ensure_collection(self, *, collection, dimension, **_k):
                env.ensured.append((collection, dimension))

            async def delete_if_exists(self, *, collection, query_filter):
                env.deleted.append((collection, query_filter))
                return 0

            async def upsert(self, *, collection, records):
                if upsert_error:
                    raise upsert_error
                env.upserts.append((collection, list(records)))
                return len(records)

            async def close(self):
                env.closed += 1

        monkeypatch.setattr("models.adapters.openrouter_provider.OpenRouterProvider", Provider)
        monkeypatch.setattr("retrieval.adapters.qdrant_store.QdrantVectorStore", Qdrant)
        monkeypatch.setattr(ip, "_vision_workspace_sensitive", lambda _w: sensitive)
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(ip, "VISION_MAX_PAGES", 5)
        monkeypatch.setattr(ip, "VISION_IMAGE_BUDGET_BYTES", 100_000)

    def run(self):
        return asyncio.run(
            ip._run_multimodal_step(
                b"%PDF", job_id=JOB, workspace_id=WS, document_id=DOC, version_id=VER
            )
        )

    @property
    def records(self):
        return [r for _c, recs in self.upserts for r in recs]


def test_flag_defaults_off() -> None:
    assert ip.MULTIMODAL_EMBED_ENABLED is False


def test_embeds_real_png_pages_with_acl_generation_and_identity(monkeypatch) -> None:
    env = _Env(monkeypatch, pages=2)
    main = threading.get_ident()
    assert env.run() == 2
    assert env.render_threads and main not in env.render_threads
    assert all(u.startswith("data:image/png;base64,") and len(u) > 30 for u in env.embedded_urls)
    coll, _ = env.upserts[0]
    assert coll == "knowledge_multimodal" and env.ensured == [("knowledge_multimodal", 3072)]
    recs = env.records
    assert [r.visual_citation.page for r in recs] == [1, 2]
    for r in recs:
        assert r.workspace_id == str(WS) and r.document_id == str(DOC)
        assert r.document_version_id == str(VER)
        assert r.metadata["embedding_dimension"] == 3072
        assert r.metadata["embedding_model"] == "google/gemini-embedding-2"
        assert {"embedding_provider", "embedding_revision"} <= set(r.metadata)
    assert len({r.id for r in recs}) == 2
    assert env.closed == 1


def test_old_points_of_the_document_are_deleted_before_upsert(monkeypatch) -> None:
    env = _Env(monkeypatch, pages=1)
    env.run()
    (coll, filt), = env.deleted
    assert coll == "knowledge_multimodal"
    assert filt.workspace_ids == [str(WS)] and filt.document_ids == [str(DOC)]


def test_ids_are_stable_across_retries(monkeypatch) -> None:
    a, b = _Env(monkeypatch, pages=1), None
    a.run()
    b = _Env(monkeypatch, pages=1)
    b.run()
    assert a.records[0].id == b.records[0].id


def test_empty_png_is_skipped_and_logged_never_embedded(monkeypatch, caplog) -> None:
    env = _Env(monkeypatch, pages=2, png=lambda p: b"" if p == 1 else b"png" * 10)
    with caplog.at_level(logging.WARNING, logger=ip.logger.name):
        assert env.run() == 1
    assert len(env.embedded_urls) == 1
    assert [r.visual_citation.page for r in env.records] == [2]
    assert any(getattr(r, "event", "") == "multimodal_page_skipped" for r in caplog.records)


def test_provider_error_skips_page_and_logs_error(monkeypatch, caplog) -> None:
    env = _Env(monkeypatch, pages=3, embed_fail_page=2)
    with caplog.at_level(logging.WARNING, logger=ip.logger.name):
        assert env.run() == 2
    assert [r.visual_citation.page for r in env.records] == [1, 3]
    failed = [r for r in caplog.records if getattr(r, "event", "") == "multimodal_page_failed"]
    assert len(failed) == 1 and "provider boom" in failed[0].error


def test_wrong_dimension_embeds_nothing_and_never_upserts(monkeypatch, caplog) -> None:
    env = _Env(monkeypatch, pages=2, vec_len=768)
    with caplog.at_level(logging.WARNING, logger=ip.logger.name):
        assert env.run() == 0
    assert env.upserts == []
    failed = [r for r in caplog.records if getattr(r, "event", "") == "multimodal_page_failed"]
    assert len(failed) == 2


def test_byte_budget_stops_rendering_extra_pages(monkeypatch) -> None:
    env = _Env(monkeypatch, pages=5)
    monkeypatch.setattr(ip, "VISION_IMAGE_BUDGET_BYTES", 120)  # 50-byte pngs
    assert env.run() == 2


def test_qdrant_upsert_failure_is_logged_not_raised(monkeypatch, caplog) -> None:
    env = _Env(monkeypatch, pages=1, upsert_error=RuntimeError("qdrant down"))
    with caplog.at_level(logging.WARNING, logger=ip.logger.name):
        assert env.run() == 0  # must NOT raise: ingest still succeeds
    failed = [r for r in caplog.records if getattr(r, "event", "") == "multimodal_step_failed"]
    assert len(failed) == 1 and "qdrant down" in failed[0].error
    assert env.closed == 1


def test_sensitive_workspace_sends_no_image(monkeypatch) -> None:
    env = _Env(monkeypatch, pages=2, sensitive=True)
    assert env.run() == 0
    assert env.embedded_urls == [] and env.render_threads == []


def test_missing_api_key_skips_without_network(monkeypatch) -> None:
    env = _Env(monkeypatch, pages=2)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert env.run() == 0
    assert env.embedded_urls == [] and env.render_threads == []
