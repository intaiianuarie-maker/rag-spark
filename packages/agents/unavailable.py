"""Typed UNAVAILABLE contract for unconfigured agent execution (T-R02).

The rule this module exists to enforce:

    An agent or research request that cannot actually be executed must NEVER
    come back looking like a completed run with invented content.

Before T-R02 the API's production factories supplied a no-op LLM that answered
every goal with the literal text ``"Final Answer: done"`` and a research
fallback that fabricated chunk ``chk-1``, evidence ``ev-1`` and
keyword-triggered unknowns/contradictions. A caller asking "what is 17x19" got
a confident, entirely fabricated answer, and the report carried citations to
chunks that were never retrieved.

The honest alternative is not silence and not a fake: it is a **structured
UNAVAILABLE** that names the missing piece. Callers can branch on a stable
error code, operators can see exactly what was not wired, and nothing is
invented.

Test doubles stay possible, but only behind explicit test-only construction —
the production factories in :mod:`app.agents.router` /
:mod:`app.agents.research` have no fake to fall back on.
"""

from __future__ import annotations

import dataclasses

from agents.interfaces import AgentErrorCode

#: Stable string carried on API responses. Kept as a plain constant so the
#: response models and the tests agree without importing the enum at runtime.
UNAVAILABLE_CODE = AgentErrorCode.UNAVAILABLE.value

#: The named pieces that can be missing. Reported verbatim so an operator can
#: see WHICH wiring is absent, rather than a generic "not configured".
MISSING_LLM_PROVIDER = "llm_provider"
MISSING_RESEARCH_PIPELINE = "research_pipeline"

KNOWN_MISSING_PIECES = frozenset({MISSING_LLM_PROVIDER, MISSING_RESEARCH_PIPELINE})


class AgentUnavailableError(RuntimeError):
    """Raised by a production factory when it has nothing real to run.

    Raised *instead of* constructing a stand-in. The caller turns this into a
    typed UNAVAILABLE response; no provider and no tool is ever invoked on the
    way out, because the object graph was never built.
    """

    def __init__(self, missing: str, detail: str) -> None:
        if missing not in KNOWN_MISSING_PIECES:
            raise ValueError(
                f"unknown missing piece {missing!r}; expected one of "
                f"{sorted(KNOWN_MISSING_PIECES)}"
            )
        super().__init__(detail)
        self.missing = missing
        self.detail = detail


@dataclasses.dataclass(frozen=True, slots=True)
class Unavailable:
    """The structured UNAVAILABLE payload, shared by the run and research paths.

    One shape for both so a caller learns the contract once. ``abstained`` is
    True because from the caller's point of view nothing was retrieved and
    nothing was produced; ``missing`` names the absent wiring.
    """

    missing: str
    detail: str

    @property
    def code(self) -> str:
        return UNAVAILABLE_CODE

    def report(self, topic: str) -> dict[str, object]:
        """Empty honesty package + the reason, with nothing invented.

        Every content list is empty on purpose: no grounded sentences, no
        citations, no evidence ids, no unknown/contradiction entries. A caller
        that sees zero citations cannot mistake this for a researched answer.
        """
        return {
            "grounded_sentences": [],
            "unknowns": [],
            "contradictions": [],
            "contradiction_outline": [],
            "citations": [],
            "abstained": True,
            "abstention_reason": f"{UNAVAILABLE_CODE}: {self.detail}",
            "searched": [topic] if topic else [],
            "limitations": [
                f"UNAVAILABLE — {self.missing} is not configured; nothing was "
                "retrieved, generated or cited.",
            ],
            "error_code": self.code,
            "missing": self.missing,
            "error": self.detail,
        }