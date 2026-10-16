"""Unit tests for T-OR-02 OpenRouter HTTP transport, retry policy & durable budget ledger."""

from __future__ import annotations

import csv
from typing import Any

import httpx
import pytest
from models.adapters.openrouter_transport import (
    LEDGER_COLUMNS,
    AttemptRecord,
    AuthenticationError,
    BudgetExceededError,
    DurableBudgetLedger,
    NonRetryableError,
    OpenRouterTransport,
    RefusalError,
    RetryableError,
    TransportError,
)

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# 1. Error Taxonomy & Hierarchy
# ---------------------------------------------------------------------------


def test_error_taxonomy_hierarchy() -> None:
    assert issubclass(RetryableError, TransportError)
    assert issubclass(NonRetryableError, TransportError)
    assert issubclass(AuthenticationError, NonRetryableError)
    assert issubclass(RefusalError, NonRetryableError)
    assert issubclass(BudgetExceededError, NonRetryableError)


def test_classify_status_codes() -> None:
    transport = OpenRouterTransport(api_key="sk-test")

    # Auth failures
    err_401 = transport.classify_status(401, "Unauthorized")
    assert isinstance(err_401, AuthenticationError)
    assert err_401.status_code == 401

    err_403 = transport.classify_status(403, "Forbidden")
    assert isinstance(err_403, AuthenticationError)

    # Budget / Balance
    err_402 = transport.classify_status(402, "Payment Required")
    assert isinstance(err_402, BudgetExceededError)

    err_insufficient = transport.classify_status(500, "Insufficient credits on account")
    assert isinstance(err_insufficient, BudgetExceededError)

    # Refusal
    err_refusal = transport.classify_status(400, "Provider content refusal policy violation")
    assert isinstance(err_refusal, RefusalError)

    # Retryable transient errors
    for code in (429, 502, 503, 504):
        err = transport.classify_status(code, "Temporary error")
        assert isinstance(err, RetryableError), f"Expected RetryableError for {code}"

    # General client errors
    err_400 = transport.classify_status(400, "Bad request parameters")
    assert isinstance(err_400, NonRetryableError)
    assert not isinstance(err_400, (AuthenticationError, RefusalError, BudgetExceededError))


# ---------------------------------------------------------------------------
# 2. Bounded Retries & Non-Retryable Fast Path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_never_retried(tmp_path: Any) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "Invalid API key"})

    mock_transport = httpx.MockTransport(handler)
    ledger = DurableBudgetLedger(tmp_path / "ledger.csv")
    transport = OpenRouterTransport(
        api_key="sk-bad",
        client=httpx.AsyncClient(transport=mock_transport),
        ledger=ledger,
        max_retries=3,
    )

    with pytest.raises(AuthenticationError) as exc_info:
        await transport.apost_json("chat/completions", {"model": "test"}, model_requested="test")

    assert calls == 1, "Auth failure must abort on attempt 1 without retry"
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_refusal_never_retried(tmp_path: Any) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "Content refusal by safety system"})

    mock_transport = httpx.MockTransport(handler)
    ledger = DurableBudgetLedger(tmp_path / "ledger.csv")
    transport = OpenRouterTransport(
        api_key="sk-test",
        client=httpx.AsyncClient(transport=mock_transport),
        ledger=ledger,
        max_retries=3,
    )

    with pytest.raises(RefusalError):
        await transport.apost_json("chat/completions", {"model": "test"}, model_requested="test")

    assert calls == 1, "Content refusal must abort immediately without retry"


@pytest.mark.asyncio
async def test_retryable_error_retries_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    async def _noop(_: float) -> None:
        pass

    monkeypatch.setattr("asyncio.sleep", _noop)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"error": "service unavailable"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.0001},
            },
        )

    mock_transport = httpx.MockTransport(handler)
    ledger = DurableBudgetLedger(tmp_path / "ledger.csv")
    transport = OpenRouterTransport(
        api_key="sk-test",
        client=httpx.AsyncClient(transport=mock_transport),
        ledger=ledger,
        max_retries=3,
        backoff_base_seconds=0.001,
    )

    data, record = await transport.apost_json(
        "chat/completions", {"model": "gemma"}, model_requested="gemma"
    )
    assert calls == 3
    assert data["choices"][0]["message"]["content"] == "ok"
    assert record.prompt_tokens == 10
    assert record.completion_tokens == 2
    assert record.cost_usd == 0.0001


@pytest.mark.asyncio
async def test_retryable_error_exhausted_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    async def _noop(_: float) -> None:
        pass

    monkeypatch.setattr("asyncio.sleep", _noop)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(504, json={"error": "gateway timeout"})

    mock_transport = httpx.MockTransport(handler)
    ledger = DurableBudgetLedger(tmp_path / "ledger.csv")
    transport = OpenRouterTransport(
        api_key="sk-test",
        client=httpx.AsyncClient(transport=mock_transport),
        ledger=ledger,
        max_retries=3,
        backoff_base_seconds=0.001,
    )

    with pytest.raises(RetryableError) as exc_info:
        await transport.apost_json("chat/completions", {"model": "gemma"}, model_requested="gemma")

    assert calls == 3
    assert exc_info.value.status_code == 504


# ---------------------------------------------------------------------------
# 3. Durable Budget Ledger Persistence
# ---------------------------------------------------------------------------


def test_durable_budget_ledger_writes_canonical_row(tmp_path: Any) -> None:
    ledger_file = tmp_path / "sub" / "budget_ledger.csv"
    ledger = DurableBudgetLedger(ledger_file)

    attempt_ok = AttemptRecord(
        model_requested="google/gemma-4-26b-a4b-it:free",
        model_used="google/gemma-4-26b-a4b-it",
        call_site="answer_service",
        status_code=200,
        latency_ms=125,
        prompt_tokens=42,
        completion_tokens=18,
        cost_usd=0.0000045,
        is_free=False,
        is_fallback=True,
        detail={"query_id": "q123"},
        run_tag="unit_test_run",
    )
    ledger.record_attempt(attempt_ok)

    attempt_fail = AttemptRecord(
        model_requested="google/gemini-embedding-2",
        model_used="google/gemini-embedding-2",
        call_site="embed_service",
        status_code=503,
        latency_ms=50,
        error="RetryableError",
        detail={"error_code": "UNAVAILABLE"},
        run_tag="unit_test_run",
    )
    ledger.record_attempt(attempt_fail)

    assert ledger_file.exists()
    with open(ledger_file, newline="", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    assert len(reader) == 3  # header + 2 rows
    assert reader[0] == LEDGER_COLUMNS
    assert len(reader[0]) == 17

    row1 = reader[1]
    assert len(row1) == 17
    assert row1[1] == "unit_test_run"
    assert row1[2] == "metered_call"
    assert row1[3] == "answer_service"
    assert row1[4] == "google/gemma-4-26b-a4b-it:free"
    assert row1[5] == "google/gemma-4-26b-a4b-it"
    assert row1[9] == "1"  # fallback
    assert row1[11] == "42"  # prompt_tokens
    assert row1[12] == "18"  # completion_tokens
    assert row1[13] == "0.00000450"  # cost_usd
    assert "q123" in row1[16]

    row2 = reader[2]
    assert len(row2) == 17
    assert row2[2] == "failed_call"
    assert row2[10] == "1"  # failed_calls
    assert row2[15] == "RetryableError"


# ---------------------------------------------------------------------------
# 4. Sync Post Execution & Headers
# ---------------------------------------------------------------------------


def test_sync_post_json_execution(tmp_path: Any) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["authorization"] == "Bearer sk-sync"
        assert request.headers["HTTP-Referer"] == "https://knowledge-os.local"
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.1] * 3072}], "usage": {"prompt_tokens": 8}},
        )

    mock_transport = httpx.MockTransport(handler)
    ledger = DurableBudgetLedger(tmp_path / "sync_ledger.csv")
    transport = OpenRouterTransport(
        api_key="sk-sync",
        sync_client=httpx.Client(transport=mock_transport),
        ledger=ledger,
    )

    data, record = transport.post_json(
        "embeddings",
        {"model": "google/gemini-embedding-2", "input": ["hello"]},
        model_requested="google/gemini-embedding-2",
        call_site="sync_test",
    )
    assert calls == 1
    assert len(data["data"][0]["embedding"]) == 3072
    assert record.prompt_tokens == 8
    assert record.call_site == "sync_test"
