"""Shared OpenRouter HTTP transport, retry policy, error classification & durable ledger (T-OR-02).

Contract and policy guarantees:
1. Shared transport: single robust transport surface for chat completions,
   embeddings, and reranking across free and paid OpenRouter routes.
2. Error classification:
   - :class:`RetryableError`: 502, 503, 504, 429 (when configured), connection/timeout errors.
   - :class:`NonRetryableError`: 400, 404, invalid schemas, client faults.
   - :class:`AuthenticationError`: 401, 403. Must NEVER trigger paid escalation.
   - :class:`RefusalError`: Model or provider content refusal. Must NEVER trigger paid escalation.
   - :class:`BudgetExceededError`: 402 / credit exhaustion / quota breaches.
3. Durable budget ledger: writes canonical 17-column rows to CSV/ledger storage
   for persistent token and cost accounting, distinguishing zero cost ($0.00) from
   unknown/metered-upstream cost.
4. Provider policy enforcement: outgoing payloads respect ``provider.only``,
   ``allow_fallbacks``, and data-handling tags resolved by the central catalogue.
"""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 90.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_RETRYABLE_STATUSES = (429, 502, 503, 504)

DEFAULT_LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "../../../../evals/benchmark_v1/reports/budget_ledger.csv",
)

LEDGER_COLUMNS = [
    "timestamp_utc",
    "run_tag",
    "row_type",
    "call_site",
    "model_requested",
    "model_used",
    "calls",
    "free_calls",
    "paid_calls",
    "fallbacks",
    "failed_calls",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "latency_ms",
    "error",
    "detail",
]


# ---------------------------------------------------------------------------
# Error Taxonomy
# ---------------------------------------------------------------------------


class TransportError(RuntimeError):
    """Base exception for OpenRouter transport failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class RetryableError(TransportError):
    """Transient errors eligible for bounded retries (502, 503, 504, 429, timeouts)."""


class NonRetryableError(TransportError):
    """Permanent errors that must abort immediately without retries or paid escalation."""


class AuthenticationError(NonRetryableError):
    """Authentication or authorization failure (401, 403). Never triggers paid fallback."""


class RefusalError(NonRetryableError):
    """Model or provider refusal. Never triggers paid fallback."""


class BudgetExceededError(NonRetryableError):
    """Account credit exhausted or budget ceiling reached (402, quota limits)."""


# ---------------------------------------------------------------------------
# Attempt Record
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class AttemptRecord:
    """Detailed record of an individual inference attempt."""

    model_requested: str
    model_used: str
    call_site: str
    status_code: int = 0
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None  # None indicates unknown/unmetered upstream
    is_free: bool = False
    is_fallback: bool = False
    error: str | None = None
    detail: dict[str, Any] = dataclasses.field(default_factory=dict)
    run_tag: str = "openrouter_transport"


# ---------------------------------------------------------------------------
# Durable Budget Ledger
# ---------------------------------------------------------------------------


class DurableBudgetLedger:
    """Thread-safe persistent ledger appending canonical 17-column accounting rows."""

    def __init__(self, file_path: str | Path | None = None) -> None:
        self.file_path = Path(file_path or DEFAULT_LEDGER_PATH).resolve()
        self._lock = threading.Lock()
        self._initialized = False

    def _ensure_file(self) -> None:
        if self._initialized and self.file_path.exists():
            return
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists() or self.file_path.stat().st_size == 0:
            with open(self.file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(LEDGER_COLUMNS)
        self._initialized = True

    def record_attempt(self, attempt: AttemptRecord) -> None:
        """Appends one attempt record to the ledger in canonical format."""
        now_iso = datetime.now(UTC).isoformat()
        cost_str = f"{attempt.cost_usd:.8f}" if attempt.cost_usd is not None else ""
        detail_str = json.dumps(attempt.detail) if attempt.detail else ""

        row = [
            now_iso,
            attempt.run_tag,
            "metered_call" if not attempt.error else "failed_call",
            attempt.call_site,
            attempt.model_requested,
            attempt.model_used,
            1,
            1 if attempt.is_free else 0,
            1 if (not attempt.is_free and attempt.model_used) else 0,
            1 if attempt.is_fallback else 0,
            1 if attempt.error else 0,
            attempt.prompt_tokens,
            attempt.completion_tokens,
            cost_str,
            attempt.latency_ms,
            attempt.error or "",
            detail_str,
        ]

        with self._lock:
            try:
                self._ensure_file()
                with open(self.file_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(row)
            except Exception as e:
                logger.warning("Failed to append row to budget ledger: %s", e)


GLOBAL_DURABLE_LEDGER = DurableBudgetLedger()


# ---------------------------------------------------------------------------
# Transport Core
# ---------------------------------------------------------------------------


class OpenRouterTransport:
    """Robust HTTP transport for OpenRouter endpoints with bounded retries and policy guards."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_OPENROUTER_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE,
        retryable_statuses: tuple[int, ...] = DEFAULT_RETRYABLE_STATUSES,
        client: httpx.AsyncClient | None = None,
        sync_client: httpx.Client | None = None,
        ledger: DurableBudgetLedger | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_base_seconds = backoff_base_seconds
        self.retryable_statuses = set(retryable_statuses)
        self.ledger = ledger or GLOBAL_DURABLE_LEDGER

        self._client = client
        self._owns_client = client is None

        self._sync_client = sync_client
        self._owns_sync_client = sync_client is None

    def build_headers(
        self,
        custom_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Builds standard authn and metadata headers."""
        headers = {
            "content-type": "application/json",
            "HTTP-Referer": "https://knowledge-os.local",
            "X-Title": "Knowledge OS",
        }
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        if custom_headers:
            headers.update(custom_headers)
        return headers

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _http_sync(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(timeout=self.timeout)
        return self._sync_client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def close(self) -> None:
        if self._sync_client is not None and self._owns_sync_client:
            self._sync_client.close()
            self._sync_client = None

    def classify_status(self, status_code: int, response_text: str) -> TransportError:
        """Classifies HTTP error status codes into typed domain exceptions."""
        lowered = response_text.lower()

        if status_code in (401, 403):
            return AuthenticationError(
                f"OpenRouter authentication failure ({status_code}): {response_text[:300]}",
                status_code=status_code,
                response_body=response_text,
            )
        if status_code == 402 or "insufficient" in lowered or "credit" in lowered:
            return BudgetExceededError(
                f"OpenRouter budget or balance exceeded ({status_code}): {response_text[:300]}",
                status_code=status_code,
                response_body=response_text,
            )
        if "refusal" in lowered or "refused" in lowered or "policy violation" in lowered:
            return RefusalError(
                f"OpenRouter model or content refusal ({status_code}): {response_text[:300]}",
                status_code=status_code,
                response_body=response_text,
            )
        if status_code in self.retryable_statuses:
            return RetryableError(
                f"OpenRouter transient error ({status_code}): {response_text[:300]}",
                status_code=status_code,
                response_body=response_text,
            )
        return NonRetryableError(
            f"OpenRouter request error ({status_code}): {response_text[:300]}",
            status_code=status_code,
            response_body=response_text,
        )

    # -----------------------------------------------------------------------
    # Async Request Execution
    # -----------------------------------------------------------------------

    async def apost_json(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        call_site: str = "default",
        model_requested: str = "",
        run_tag: str = "openrouter_transport",
    ) -> tuple[dict[str, Any], AttemptRecord]:
        """Executes an async POST request with bounded retries and ledger recording."""
        url = f"{self.base_url}/{endpoint_path.lstrip('/')}"
        final_headers = self.build_headers(headers)
        client = self._http()
        target_model = payload.get("model", model_requested)

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            start_time = time.monotonic()
            try:
                resp = await client.post(url, json=payload, headers=final_headers)
                latency_ms = int((time.monotonic() - start_time) * 1000)

                if resp.status_code != 200:
                    classified = self.classify_status(resp.status_code, resp.text)
                    if isinstance(classified, RetryableError) and attempt < self.max_retries:
                        await asyncio.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                        continue
                    # Non-retryable or exhausted retries
                    record = AttemptRecord(
                        model_requested=model_requested or target_model,
                        model_used=target_model,
                        call_site=call_site,
                        status_code=resp.status_code,
                        latency_ms=latency_ms,
                        error=type(classified).__name__,
                        detail={"status": resp.status_code, "body": resp.text[:400]},
                        run_tag=run_tag,
                    )
                    self.ledger.record_attempt(record)
                    raise classified

                data = resp.json()
                usage = data.get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
                cost_val = usage.get("cost")
                cost_usd = float(cost_val) if cost_val is not None else None

                record = AttemptRecord(
                    model_requested=model_requested or target_model,
                    model_used=target_model,
                    call_site=call_site,
                    status_code=resp.status_code,
                    latency_ms=latency_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    cost_usd=cost_usd,
                    is_free=target_model.endswith(":free"),
                    run_tag=run_tag,
                )
                self.ledger.record_attempt(record)
                return data, record

            except (httpx.RequestError, TimeoutError) as e:
                latency_ms = int((time.monotonic() - start_time) * 1000)
                last_error = e
                if attempt < self.max_retries:
                    await asyncio.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                    continue

                record = AttemptRecord(
                    model_requested=model_requested or target_model,
                    model_used=target_model,
                    call_site=call_site,
                    status_code=0,
                    latency_ms=latency_ms,
                    error=type(e).__name__,
                    detail={"exception": str(e)},
                    run_tag=run_tag,
                )
                self.ledger.record_attempt(record)
                raise RetryableError(f"OpenRouter network/timeout error: {e}") from e

        record = AttemptRecord(
            model_requested=model_requested or target_model,
            model_used=target_model,
            call_site=call_site,
            status_code=0,
            latency_ms=0,
            error="ExhaustedRetries",
            detail={"last_error": str(last_error)},
            run_tag=run_tag,
        )
        self.ledger.record_attempt(record)
        raise RetryableError(f"OpenRouter request exhausted retries: {last_error}")

    # -----------------------------------------------------------------------
    # Sync Request Execution
    # -----------------------------------------------------------------------

    def post_json(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        call_site: str = "default",
        model_requested: str = "",
        run_tag: str = "openrouter_transport",
    ) -> tuple[dict[str, Any], AttemptRecord]:
        """Executes a sync POST request with bounded retries and ledger recording."""
        url = f"{self.base_url}/{endpoint_path.lstrip('/')}"
        final_headers = self.build_headers(headers)
        client = self._http_sync()
        target_model = payload.get("model", model_requested)

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            start_time = time.monotonic()
            try:
                resp = client.post(url, json=payload, headers=final_headers)
                latency_ms = int((time.monotonic() - start_time) * 1000)

                if resp.status_code != 200:
                    classified = self.classify_status(resp.status_code, resp.text)
                    if isinstance(classified, RetryableError) and attempt < self.max_retries:
                        time.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                        continue
                    record = AttemptRecord(
                        model_requested=model_requested or target_model,
                        model_used=target_model,
                        call_site=call_site,
                        status_code=resp.status_code,
                        latency_ms=latency_ms,
                        error=type(classified).__name__,
                        detail={"status": resp.status_code, "body": resp.text[:400]},
                        run_tag=run_tag,
                    )
                    self.ledger.record_attempt(record)
                    raise classified

                data = resp.json()
                usage = data.get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
                cost_val = usage.get("cost")
                cost_usd = float(cost_val) if cost_val is not None else None

                record = AttemptRecord(
                    model_requested=model_requested or target_model,
                    model_used=target_model,
                    call_site=call_site,
                    status_code=resp.status_code,
                    latency_ms=latency_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    cost_usd=cost_usd,
                    is_free=target_model.endswith(":free"),
                    run_tag=run_tag,
                )
                self.ledger.record_attempt(record)
                return data, record

            except (httpx.RequestError, TimeoutError) as e:
                latency_ms = int((time.monotonic() - start_time) * 1000)
                last_error = e
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                    continue

                record = AttemptRecord(
                    model_requested=model_requested or target_model,
                    model_used=target_model,
                    call_site=call_site,
                    status_code=0,
                    latency_ms=latency_ms,
                    error=type(e).__name__,
                    detail={"exception": str(e)},
                    run_tag=run_tag,
                )
                self.ledger.record_attempt(record)
                raise RetryableError(f"OpenRouter network/timeout error: {e}") from e

        record = AttemptRecord(
            model_requested=model_requested or target_model,
            model_used=target_model,
            call_site=call_site,
            status_code=0,
            latency_ms=0,
            error="ExhaustedRetries",
            detail={"last_error": str(last_error)},
            run_tag=run_tag,
        )
        self.ledger.record_attempt(record)
        raise RetryableError(f"OpenRouter request exhausted retries: {last_error}")
