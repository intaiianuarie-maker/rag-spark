"""Hardening tests for the opt-in VISION ingest step (plain sync; asyncio.run)."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import threading
import uuid

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters import pdfium_render
from jobs import ingest_pipeline as ip
from models.vision import DEFAULT_VISION_FREE_MODEL, VisionPolicyError

pytestmark = pytest.mark.unit

WS = uuid.uuid4()


def _png(page: int, size: int = 100) -> bytes:
    return f"png-{page}".encode().ljust(size, b"x")


class _Env:
    """Patches rendering + transport; records what happened."""

    def __init__(self, monkeypatch, *, pages=3, fail_page=None, png_size=100):
        self.pages = pages
        self.calls: list[dict] = []
        self.render_threads: list[int] = []
        self.count_threads: list[int] = []
        self.transports: list[tuple] = []
        env = self

        def count(_content):
            env.count_threads.append(threading.get_ident())
            return pages

        def render(_content, page, dpi=150):
            env.render_threads.append(threading.get_ident())
            return _png(page, png_size), 612.0, 792.0, 1, 1

        class Transport:
            def __init__(self, base_url, api_key, model):
                env.transports.append((base_url, api_key, model))

            async def post_completion(self, payload):
                env.calls.append(payload)
                if fail_page is not None and base64.b64encode(
                    _png(fail_page, png_size)
                ).decode() in str(payload):
                    raise RuntimeError("boom")
                return {"choices": [{"message": {"content": "a chart"}}]}

        monkeypatch.setattr(pdfium_render, "get_pdf_page_count", count)
        monkeypatch.setattr(pdfium_render, "render_pdf_page", render)
        monkeypatch.setattr(ip, "_OpenRouterVisionTransport", Transport)
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(ip, "VISION_MAX_PAGES", 5)
        monkeypatch.setattr(ip, "VISION_IMAGE_BUDGET_BYTES", 10_000)

    def run(self, sensitive=False):
        return asyncio.run(
            ip._vision_elements_for_pdf(
                b"%PDF",
                document=None,
                workspace_id=WS,
                sensitive=sensitive,
                pages=range(1, self.pages + 1),
            )
        )


def _events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def test_rendering_runs_off_the_event_loop_thread(monkeypatch) -> None:
    env = _Env(monkeypatch, pages=2)
    main = threading.get_ident()
    assert len(env.run()) == 2
    assert env.render_threads and main not in env.render_threads
    assert env.count_threads and main not in env.count_threads


def test_one_failing_page_is_skipped_others_kept(monkeypatch, caplog) -> None:
    env = _Env(monkeypatch, pages=3, fail_page=2)
    with caplog.at_level(logging.WARNING, logger=ip.logger.name):
        elements = env.run()
    assert [e.page for e in elements] == [1, 3]
    failed = _events(caplog, "vision_page_failed")
    assert len(failed) == 1 and failed[0].page == 2


def test_budget_stop_keeps_earlier_pages_and_logs_skipped(monkeypatch, caplog) -> None:
    _Env(monkeypatch, pages=5)
    monkeypatch.setattr(ip, "VISION_IMAGE_BUDGET_BYTES", 250)
    with caplog.at_level(logging.INFO, logger=ip.logger.name):
        elements = asyncio.run(
            ip._vision_elements_for_pdf(
                b"%PDF", document=None, workspace_id=WS, sensitive=False, pages=range(1, 6)
            )
        )
    assert [e.page for e in elements] == [1, 2]
    stop = _events(caplog, "vision_budget_stop")
    assert len(stop) == 1 and stop[0].skipped_pages == [3, 4, 5]


def test_text_has_model_generated_prefix_and_vision_metadata(monkeypatch) -> None:
    elements = _Env(monkeypatch, pages=1).run()
    el = elements[0]
    assert el.text == "[Model-generated page description, page 1] a chart"
    assert el.metadata["extraction"] == "vision"
    assert el.metadata["vision_model"] == DEFAULT_VISION_FREE_MODEL


def test_models_come_from_vision_module(monkeypatch) -> None:
    env = _Env(monkeypatch, pages=1)
    env.run()
    assert env.transports[0][2] == DEFAULT_VISION_FREE_MODEL
    assert env.calls[0]["model"] == DEFAULT_VISION_FREE_MODEL
    assert not hasattr(ip, "VISION_MODEL_FREE") and not hasattr(ip, "VISION_MODEL_PAID")


def test_sensitive_workspace_never_sends_an_image(monkeypatch) -> None:
    env = _Env(monkeypatch, pages=2)
    with pytest.raises(VisionPolicyError):
        env.run(sensitive=True)
    assert env.calls == []  # no free OR paid HTTP call


def test_sensitive_lookup_fails_closed(monkeypatch) -> None:
    from models import router_config

    def boom(_ws):
        raise RuntimeError("config store down")

    monkeypatch.setattr(router_config, "load_workspace_config", boom)
    assert ip._vision_workspace_sensitive(str(WS)) is True
    monkeypatch.setattr(router_config, "load_workspace_config", lambda _ws: None)
    assert ip._vision_workspace_sensitive(str(WS)) is False


def test_missing_api_key_skips_with_one_warning_and_no_http(monkeypatch, caplog) -> None:
    env = _Env(monkeypatch, pages=2)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with caplog.at_level(logging.WARNING, logger=ip.logger.name):
        assert env.run() == []
    assert env.calls == [] and env.transports == [] and env.render_threads == []
    assert len(_events(caplog, "vision_no_api_key")) == 1


@pytest.mark.parametrize("raw", ["abc", "", "0", "-3", "1.5"])
def test_bad_env_int_falls_back_to_default(monkeypatch, raw) -> None:
    monkeypatch.setenv("VISION_X", raw)
    assert ip._env_int("VISION_X", 7) == 7


def test_good_env_int_is_used(monkeypatch) -> None:
    monkeypatch.setenv("VISION_X", " 9 ")
    assert ip._env_int("VISION_X", 7) == 9
