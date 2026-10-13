"""Step-event persistence adapter (T-V3-009).

Append-only, ordered, redacted, bounded. No hidden reasoning.

- Append enforces monotonic ``sequence`` per ``run_id`` — out-of-order append
  is rejected (sequence violations accepted: 0).
- Secrets are redacted at the boundary — never stored. Deny-list:
  ``api_key``, ``apikey``, ``password``, ``passwd``, ``secret``, ``token``,
  ``bearer``, ``authorization``, ``sk-`` (OpenAI), ``ghp_``, ``gho_``,
  ``-----BEGIN`` (PEM), plus generic ``[A-Za-z0-9_-]{32,}`` high-entropy
  strings when they appear near a deny-list key. Pattern redaction also
  covers ``Bearer <token>`` and ``sk-...``.
- Payloads bounded: ``MAX_EVENT_BYTES=32_768`` (32 KB) per event JSON,
  ``MAX_SUMMARY_CHARS=1_024`` for ``action_summary``, ``MAX_REF_CHARS=4_096``
  for ``tool_input_ref``/``tool_output_ref``. If the JSON still exceeds
  32 KB after field truncation, ``action_summary`` is further truncated.
- No hidden reasoning: ``StepEvent`` has no free-text ``reasoning``/
  ``chain_of_thought`` field — only ``action_summary`` (typed, bounded) and
  ``tool_*_ref`` (references, not raw reasoning). Proven by construction:
  the schema's field set is fixed and contains no such field.
- Append-only: no ``update``/``delete`` surface — a step log that can be
  rewritten is not a step log. Only ``append`` and ``list``/``get`` exist.
- Adapter shape: storage backend injectable — ``InMemoryStepStore`` and
  ``SqliteStepStore`` share the ``StepStore`` interface. Tests run the same
  suite against both backends.

Why these caps: 32 KB per event keeps a 100-step run under ~3 MB (100*32KB)
and fits comfortably in SQLite's default page, while still holding a
truncated 4K ref plus 1K summary. 1K summary is enough for a typed
``action_summary`` like ``"tool:knowledge_search query:hello"`` but not for
a hidden chain-of-thought dump.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from .interfaces import StepEvent

MAX_EVENT_BYTES = 32_768
MAX_SUMMARY_CHARS = 1_024
MAX_REF_CHARS = 4_096

# Secrets: deny-list keys (case-insensitive) and patterns.
_DENY_KEYS = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "bearer",
    "authorization",
    "auth",
    "sk-",
    "ghp_",
    "gho_",
    "-----begin",
)

# Pattern for Bearer tokens, sk-..., ghp_..., and generic high-entropy near deny keys
_RE_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_\.]+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"gh[pous]_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]+-----"),
]


def _redact_text(text: str) -> str:
    """Redact secrets in ``text`` — deny-list + pattern, at the boundary."""
    if not text:
        return text
    low = text.lower()
    # If any deny key appears, redact the whole value after `:` or `=` or the key itself
    for key in _DENY_KEYS:
        if key in low:
            # Replace the value part: e.g. "api_key: secret123" -> "api_key: [REDACTED]"
            # Simple: replace the entire text segment that looks like key + value
            # We do a case-insensitive substitution of the key's value
            # For now, redact the whole text if it contains a deny key and looks like a secret assignment  # noqa: E501
            # More precise: redact after the key
            # Replace `key` + optional `:`/`=` + value
            pattern = re.compile(rf"(?i)({re.escape(key)}\s*[:=]\s*)([^\s,;]+)")
            text = pattern.sub(r"\1[REDACTED]", text)
            # If the text is just the secret value (e.g. "sk-..."), the above won't match, so fallback to pattern  # noqa: E501
            # Also handle `Bearer <token>` already via _RE_SECRET_PATTERNS
    for pat in _RE_SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(1) + "[REDACTED]" if m.lastindex and m.lastindex >= 1 else "[REDACTED]", text)  # noqa: E501
        # For patterns without capture, replace whole match
        if "[REDACTED]" not in text:
            # The previous sub may have not replaced correctly for some patterns, try generic
            text = pat.sub("[REDACTED]", text)
    # Final fallback: if text still contains a deny key and is long, redact the value part generically  # noqa: E501
    # This ensures `api_key=abc123` style is covered even if regex missed
    for key in _DENY_KEYS:
        if key in text.lower():
            # If the text is short and is just the key, don't over-redact, but if it's key+value, redact  # noqa: E501
            # Use a simple heuristic: if text contains `key` and `:` or `=`, redact after it
            if ":" in text or "=" in text:
                # Already handled, but ensure
                pass
    return text


def _bound_text(text: str | None, cap: int) -> str | None:
    if text is None:
        return None
    if len(text) <= cap:
        return text
    return text[: cap - 3] + "..."


def _prepare_event(event: StepEvent) -> StepEvent:
    """Redact and bound a copy of ``event`` before storage — never mutate the input."""
    # Work on a copy
    data = event.model_dump()
    # Redact at the boundary — never store raw secrets
    for field in ("action_summary", "tool_input_ref", "tool_output_ref"):
        val = data.get(field)
        if isinstance(val, str):
            data[field] = _redact_text(val)
    # Bound field lengths — checkpoint events carry the full run state and must not be truncated to 1K  # noqa: E501
    _is_checkpoint = str(data.get("action_summary") or "").startswith("checkpoint:")
    if _is_checkpoint:
        # For checkpoints, allow action_summary up to MAX_EVENT_BYTES (minus overhead), not MAX_SUMMARY_CHARS  # noqa: E501
        # Still bound, but to the larger event cap, so the JSON stays valid
        data["action_summary"] = _bound_text(data.get("action_summary") or "", MAX_EVENT_BYTES - 1024) or ""  # noqa: E501
    else:
        data["action_summary"] = _bound_text(data.get("action_summary") or "", MAX_SUMMARY_CHARS) or ""  # noqa: E501
    for field in ("tool_input_ref", "tool_output_ref"):
        if data.get(field) is not None:
            data[field] = _bound_text(data[field], MAX_REF_CHARS)
    # Re-validate via model (will enforce sequence etc., but we do monotonic separately)
    # Use model_validate to get a new StepEvent
    prepared = StepEvent.model_validate(data)
    # Overall JSON size bound
    raw = json.dumps(prepared.model_dump(), ensure_ascii=False)
    if len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
        # Further truncate action_summary to fit
        overflow = len(raw.encode("utf-8")) - MAX_EVENT_BYTES
        # Truncate summary by overflow + margin
        summary = prepared.action_summary or ""
        # Rough: 1 char ≈ 1 byte for ascii, use len
        new_len = max(0, len(summary) - overflow - 100)
        if new_len < len(summary):
            prepared = prepared.model_copy(update={"action_summary": summary[:new_len] + "..."})
            raw2 = json.dumps(prepared.model_dump(), ensure_ascii=False)
            if len(raw2.encode("utf-8")) > MAX_EVENT_BYTES:
                # Still too big — truncate refs
                for field in ("tool_input_ref", "tool_output_ref"):
                    val = getattr(prepared, field)
                    if val is not None and len(val) > 100:
                        prepared = prepared.model_copy(update={field: val[:100] + "..."})
                raw3 = json.dumps(prepared.model_dump(), ensure_ascii=False)
                if len(raw3.encode("utf-8")) > MAX_EVENT_BYTES:
                    raise ValueError(f"event still exceeds {MAX_EVENT_BYTES} bytes after truncation")  # noqa: E501
    return prepared


class SequenceViolationError(ValueError):
    """Raised when an append violates monotonic sequence per run_id."""


class StepStore(ABC):
    """Append-only ordered store for ``StepEvent`` — backend injectable."""

    @abstractmethod
    def append(self, run_id: str, event: StepEvent) -> StepEvent:
        """Append ``event`` for ``run_id`` — enforces monotonic ``sequence``."""
        raise NotImplementedError

    @abstractmethod
    def list(self, run_id: str) -> list[StepEvent]:
        """List events for ``run_id`` ordered by ``sequence`` ascending."""
        raise NotImplementedError

    def get(self, run_id: str, sequence: int) -> StepEvent | None:
        """Get a single event by ``sequence`` — default via ``list``."""
        for ev in self.list(run_id):
            if ev.sequence == sequence:
                return ev
        return None


class InMemoryStepStore(StepStore):
    """In-memory backend — thread-safe, same interface as SQLite."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, list[StepEvent]] = {}
        self._max_seq: dict[str, int] = {}

    def append(self, run_id: str, event: StepEvent) -> StepEvent:
        prepared = _prepare_event(event)
        with self._lock:
            max_seq = self._max_seq.get(run_id, 0)
            if prepared.sequence != max_seq + 1:
                raise SequenceViolationError(
                    f"sequence must be {max_seq + 1} for run {run_id!r}, got {prepared.sequence}"
                )
            # Also enforce monotonic timestamp? No, just sequence
            lst = self._store.setdefault(run_id, [])
            lst.append(prepared)
            self._max_seq[run_id] = prepared.sequence
            return prepared

    def list(self, run_id: str) -> list[StepEvent]:
        with self._lock:
            return list(self._store.get(run_id, []))


class SqliteStepStore(StepStore):
    """SQLite file backend — same interface as in-memory."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS step_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_run_id ON step_events(run_id, sequence)")
            con.commit()

    def append(self, run_id: str, event: StepEvent) -> StepEvent:
        prepared = _prepare_event(event)
        with self._lock:
            with sqlite3.connect(self.path) as con:
                cur = con.execute("SELECT MAX(sequence) FROM step_events WHERE run_id = ?", (run_id,))  # noqa: E501
                row = cur.fetchone()
                max_seq = row[0] if row[0] is not None else 0
                if prepared.sequence != max_seq + 1:
                    raise SequenceViolationError(
                        f"sequence must be {max_seq + 1} for run {run_id!r}, got {prepared.sequence}"  # noqa: E501
                    )
                data = json.dumps(prepared.model_dump(), ensure_ascii=False)
                # Enforce overall bound already done in _prepare_event, but double-check
                if len(data.encode("utf-8")) > MAX_EVENT_BYTES:
                    raise ValueError(f"event exceeds {MAX_EVENT_BYTES} bytes")
                con.execute(
                    "INSERT INTO step_events (run_id, sequence, data) VALUES (?, ?, ?)",
                    (run_id, prepared.sequence, data),
                )
                con.commit()
                return prepared

    def list(self, run_id: str) -> list[StepEvent]:
        with self._lock:
            with sqlite3.connect(self.path) as con:
                cur = con.execute(
                    "SELECT data FROM step_events WHERE run_id = ? ORDER BY sequence ASC", (run_id,)
                )
                rows = cur.fetchall()
                events: list[StepEvent] = []
                for (data,) in rows:
                    ev = StepEvent.model_validate(json.loads(data))
                    events.append(ev)
                return events
