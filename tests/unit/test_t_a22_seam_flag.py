from __future__ import annotations

from packages.answer.factory import create_orchestrator


class FakeProvider:
    def __init__(self, **kwargs):
        pass


def test_rerank_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("RERANK_ENABLED", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    orch = create_orchestrator(model="m", sensitive=False)
    assert orch.reranker is None
    assert orch.rerank_enabled is False


def test_rerank_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("RERANK_ENABLED", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setattr("rerank.openrouter_provider.CohereRerankProvider", FakeProvider)
    orch = create_orchestrator(model="m", sensitive=False)
    assert orch.reranker is not None
    assert orch.rerank_enabled is True
