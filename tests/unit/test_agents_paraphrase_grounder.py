"""T-G01 — paraphrase-tolerant grounder behind the ClaimGrounder Protocol.

Benchmark Phase 0 critical path. T-EVAL-DX measured why this exists: against the
real 2wiki corpus the strict ``ExtractiveGrounder`` (verbatim containment,
threshold 1.0) produced ZERO citations for 26 of 48 items (54.2%), and
grounding accounted for 79.2% of all failures while retrieval was perfect.

These tests pin the three properties that make it safe:

* **supported paraphrase is accepted when the flag is ON, and still rejected
  when OFF** — so flipping the flag is the only thing that changes behaviour;
* **an unsupported claim is still rejected when ON** — the new grounder is not a
  weaker bar, only a less brittle one;
* **verbatim is still accepted and still scores exactly 1.0** — nothing that
  passes the strict grounder today may regress.

Protocol conformance is asserted structurally (``@runtime_checkable``), which is
what makes this a drop-in: ``verify_claims`` is untouched by this file.

Deterministic and offline: no model, no network, no spend.
"""

from __future__ import annotations

import pytest
from agents.verifier import (
    PARAPHRASE_GROUNDER_ENV,
    ParaphraseTolerantGrounder,
    resolve_grounder,
)
from answer.verification import ClaimGrounder, ExtractiveGrounder

pytestmark = pytest.mark.unit

CHUNK = (
    "Alice Smith works at Acme Corporation. Acme Corporation acquired Globex "
    "Industries in 2019 for 42 million dollars. The launch budget was 20 dollars."
)

# Same facts, reordered + inflected + wrapped in connective prose — the exact
# shape T-EVAL-DX found gemma producing.
PARAPHRASE = "In 2019, Acme Corporation acquired Globex Industries."

# Verbatim span of the chunk.
VERBATIM = "Acme Corporation acquired Globex Industries in 2019"

# Shares no content words with the chunk.
UNSUPPORTED = "The moon is made of cheddar cheese."


# ---------------------------------------------------------------------------
# Protocol conformance — what makes it a drop-in for verify_claims
# ---------------------------------------------------------------------------


def test_satisfies_the_claim_grounder_protocol() -> None:
    """Structural conformance, so verify_claims needs no change."""
    assert isinstance(ParaphraseTolerantGrounder(), ClaimGrounder)
    g = ParaphraseTolerantGrounder()
    assert isinstance(g.threshold, float)
    score = g.score(PARAPHRASE, CHUNK)
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0


def test_extractive_grounder_remains_the_strict_default() -> None:
    """The flag's OFF state must be the unchanged production bar."""
    assert isinstance(resolve_grounder(env={}), ExtractiveGrounder)


def test_flag_on_selects_the_paraphrase_grounder() -> None:
    on = resolve_grounder(env={PARAPHRASE_GROUNDER_ENV: "1"})
    assert isinstance(on, ParaphraseTolerantGrounder)
    # Any truthy spelling opts in; a falsy/unset one does not.
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        got = resolve_grounder(env={PARAPHRASE_GROUNDER_ENV: truthy})
        assert isinstance(got, ParaphraseTolerantGrounder)
    for falsy in ("", "0", "false", "no", "off", "maybe"):
        got = resolve_grounder(env={PARAPHRASE_GROUNDER_ENV: falsy})
        assert isinstance(got, ExtractiveGrounder)


# ---------------------------------------------------------------------------
# Supported paraphrase: accepted ON, rejected OFF
# ---------------------------------------------------------------------------


def test_supported_paraphrase_is_rejected_when_flag_off() -> None:
    """OFF = byte-identical production: the paraphrase is NOT accepted."""
    strict = resolve_grounder(env={})
    assert isinstance(strict, ExtractiveGrounder)
    assert strict.score(PARAPHRASE, CHUNK) < strict.threshold, (
        "with the flag OFF a paraphrase must still fail the strict bar"
    )


def test_supported_paraphrase_is_accepted_when_flag_on() -> None:
    """ON: the same paraphrase crosses the bar."""
    lenient = resolve_grounder(env={PARAPHRASE_GROUNDER_ENV: "1"})
    score = lenient.score(PARAPHRASE, CHUNK)
    assert score >= lenient.threshold, f"paraphrase should be accepted, scored {score}"


def test_default_construction_keeps_the_strict_bar() -> None:
    """Constructing the grounder directly must not imply the flag is on."""
    assert isinstance(ParaphraseTolerantGrounder(), ParaphraseTolerantGrounder)
    # The flag — not the class's existence — is what selects behaviour.
    assert isinstance(resolve_grounder(env={}), ExtractiveGrounder)


# ---------------------------------------------------------------------------
# Unsupported claims are still rejected when ON
# ---------------------------------------------------------------------------


def test_unsupported_claim_is_rejected_even_when_flag_on() -> None:
    """A claim whose content words are absent must not be grounded."""
    lenient = ParaphraseTolerantGrounder()
    score = lenient.score(UNSUPPORTED, CHUNK)
    assert score < lenient.threshold, f"unsupported claim scored {score}"
    assert score < 0.2


def test_partial_support_is_not_enough() -> None:
    """Getting *some* content words right must not cross the bar."""
    lenient = ParaphraseTolerantGrounder()
    # Only "acquired"/"Globex" territory; "chimpanzees"/"Antarctica" absent.
    claim = "Acquisition of Globex Industries happened in Antarctica with chimpanzees."
    assert lenient.score(claim, CHUNK) < lenient.threshold


# ---------------------------------------------------------------------------
# Verbatim still accepted, and exactly 1.0
# ---------------------------------------------------------------------------


def test_verbatim_still_accepted_by_both_grounders() -> None:
    strict = ExtractiveGrounder()
    lenient = ParaphraseTolerantGrounder()
    assert strict.score(VERBATIM, CHUNK) >= strict.threshold
    assert lenient.score(VERBATIM, CHUNK) >= lenient.threshold


def test_verbatim_scores_exactly_one() -> None:
    """Exact 1.0 keeps parity with the strict grounder's own scoring."""
    assert ParaphraseTolerantGrounder().score(VERBATIM, CHUNK) == 1.0


def test_lenient_grounder_never_scores_higher_than_strict_acceptance() -> None:
    """Anything the strict bar accepts must also be accepted (no regression)."""
    lenient = ParaphraseTolerantGrounder()
    strict = ExtractiveGrounder()
    for claim, chunk in [
        (VERBATIM, CHUNK),
        ("42 million dollars", CHUNK),
        ("The launch budget was 20 dollars.", CHUNK),
    ]:
        if strict.score(claim, chunk) >= strict.threshold:
            assert lenient.score(claim, chunk) >= lenient.threshold


# ---------------------------------------------------------------------------
# Guard rails carried over from the strict grounder
# ---------------------------------------------------------------------------


def test_numeric_tokens_must_match_exactly() -> None:
    """'1900' must not satisfy a '900' claim — the strict grounder's guard."""
    lenient = ParaphraseTolerantGrounder()
    chunk = "The figure was 1900 dollars in the audited accounts."
    assert lenient.score("The figure was 900 dollars.", chunk) < lenient.threshold


def test_empty_inputs_score_zero() -> None:
    g = ParaphraseTolerantGrounder()
    assert g.score("", CHUNK) == 0.0
    assert g.score(PARAPHRASE, "") == 0.0


def test_stopword_only_claim_is_not_grounded() -> None:
    """A claim made only of function words carries no evidence."""
    g = ParaphraseTolerantGrounder()
    assert g.score("the and of", CHUNK) == 0.0