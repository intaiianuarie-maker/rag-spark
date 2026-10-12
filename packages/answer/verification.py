"""Citation verification (TASK-M4-002).

A claim's ``chunk_id`` is valid only if it resolves to a chunk that **exists**,
is **inside the caller's workspace scope**, and **substantiates the claim**.
The first two are existence/scope checks; the third is **grounding** — the
claim's text must be supported by the chunk the model actually saw in context.
An optional :class:`~answer.interfaces.ChunkLookup` adds a store-backed
existence check for ids that were not in the retrieved top-k.

When a :class:`ClaimGrounder` is configured (the engine wires one by default),
a citation is added only when the cited chunk grounds the claim; citations to
chunks that do not support the claim are dropped, and a claim left with no
grounded citations is dropped too. Lookup-resolved citations (chunk not in the
retrieved context) cannot be grounded — there is no chunk content to score —
so they are dropped when a grounder is configured, keeping the verified set
strictly to chunks that were retrieved *and* support the claim.

**v1 interim strategy — conservative extractive grounding with a
context-preserving quotation mode.** The default :class:`ExtractiveGrounder`
considers a claim grounded only when its text appears **verbatim** —
case-insensitive, contiguous, and **word-bounded** — in a single sentence of
the chunk, and that sentence *asserts* the claim as fact. A sentence does
**not** assert the claim when it negates it ("X is not…"), conditionally
withholds it ("If X…"), or merely reports/contests it as attributed speech.
Attribution is detected as four **frames**, not by an open-ended adjective
denylist: (1) *quoted* — a quotation mark abuts the claim (`"X," said Alice.`);
(2) *colon* — a colon introduces it (`Alice challenged the assertion: X`);
(3) *fronted* — a comma precedes it (`According to Alice, X`); and (4)
*indirect* — a reporting verb frames it as content (`Alice said X`,
`X, said Alice`). Frames 1-3 are positional; frame 4 uses a *closed* class of
English reporting verbs, the minimal lexical knowledge that identifies an
indirect report. Together these preserve the complete attributed context
instead of asserting an arbitrary extracted clause as verified fact.
Word boundaries prevent partial-numeric sub-matching ("900" vs "1900 dollars").
Citation spans are computed on the **original** text (case-insensitive
matching via ``re.IGNORECASE``), never on a ``casefold()`` copy, so offsets
stay valid when case-folding would expand characters (``ß`` -> ``ss``).
This is deliberately strict: it rejects the support-vs-contradiction failures a
token-overlap or bare-substring scorer cannot see (negation stripping,
entity/role swap, quantity/date changes, conditionals, quoted/attributed
speech, partial-numeric values), and it never grants HIGH on paraphrase
it cannot verify; such claims abstain at INSUFFICIENT_EVIDENCE.

Because a translation's claim text is *not* a substring of a source in a
different language, cross-language answers are **not** accepted as grounded by
this interim mode; that is the separate multilingual release gate (EN→RO/PT
abstains and is disclosed as a known, unmet v1 limitation). A future semantic
(embedding) grounder can replace this behind the unchanged
:class:`ClaimGrounder` interface.

No citation is ever fabricated: an invalid id is dropped, and a claim left with
no valid ids is dropped from the output.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from retrieval.interfaces import RetrievedItem

from .interfaces import ChunkLookup
from .models import Citation, Claim

# Conservative sentence splitter (bodies only; trailing run of . ! ? stripped).
# We use sentences only to *localise* a verbatim claim so that negation /
# conditional context inside the SAME sentence can flip the verdict to abstain.
# The regex respects decimal points: does NOT split on a period preceded by a digit,
# so "1.25" stays whole instead of becoming "1" + "25".
_SENTENCE_BOUNDARY = re.compile(r"(?<![0-9])[.!?]+(?:\s+|$)")

# Whole-word, case-insensitive commitment markers for negation and rejection.
# If a sentence contains the verbatim claim AND any of these tokens, the
# sentence's commitment to the claim is negated, so we do NOT grant HIGH on it
# (we may still look at another, clean sentence). This list is deliberately
# conservative: abstaining on a marked sentence is the safe v1 interim
# behaviour ("substring occurrence is not factual support"). Attribution /
# quotation frames are detected structurally by _is_attributed_frame below,
# not by appending verbs here.
_NEGATION = re.compile(
    r"\b(?:not|no|never|none|nothing|nowhere|neither|nor|without|falsely|denied|denies|deny|denial|refutes|refute|refuted|contradicts|contradict|contradicted|rejected|rejects|reject|refused|refuse|refuses|false|cannot|can't|"
    r"isn't|aren't|wasn't|weren't|don't|doesn't|didn't|won't|wouldn't|couldn't|shouldn't|"
    r"mightn't|mustn't|hasn't|haven't|hadn't)\b"
)
# Context-preserving attribution frames. A claim is grounded as the source's
# OWN assertion only when it is NOT embedded in one of these frames, each of
# which means the source is quoting / reporting / contesting the claim rather
# than asserting it:
#   1. quoted   - a quotation mark abuts the claim ('"X," said Alice.');
#   2. colon    - a colon introduces the claim ('the assertion: X');
#   3. fronted  - a comma precedes the claim ('According to Alice, X');
#   4. indirect - a reporting verb frames the claim as its content
#                 ('Alice said X', 'X, said Alice').
# Frames 1-3 are purely positional (quote / colon / comma). Frame 4 encodes
# indirect speech, whose defining feature is the speech predicate itself, so it
# uses a *closed* class of English reporting verbs (the minimal lexical
# knowledge needed to recognise a report) - deliberately not an open-ended
# attribution-adjective denylist. Together these preserve the complete
# attributed context instead of asserting an extracted clause as verified fact.
#
# Note: "report" and "note" are included in this closed class for direct
# attribution detection ("Alice reported X", "Alice noted X"). False positives
# from noun usage ("In the report, ...", "In the note, ...") are prevented by
# _SUBORDINATOR in Frame 3 (pre-comma text starting with "In") and the
# proximity check in Frame 4 (reporting verb must be immediately adjacent to
# the claim, not in a distant clause).
_QUOTE_CHARS = "\"'\u201c\u201d\u2018\u2019"


_REPORTING_VERB = re.compile(
    r"\b(?:said|says|saying|tell|tells|told|telling|"
    r"state|states|stated|stating|claim|claims|claimed|claiming|"
    r"write|writes|wrote|written|writing|argue|argues|argued|arguing|"
    r"assert|asserts|asserted|asserting|remark|remarks|remarked|remarking|"
    r"explain|explains|explained|explaining|note|notes|noted|noting|"
    r"suggest|suggests|suggested|suggesting|report|reports|reported|reporting|"
    r"announce|announces|announced|announcing|declare|declares|declared|declaring|"
    r"mention|mentions|mentioned|mentioning|respond|responds|responded|responding|"
    r"reply|replies|replied|replying|allege|alleges|alleged|alleging|"
    r"indicate|indicates|indicated|indicating|observe|observes|observed|observing|"
    r"insist|insists|insisted|insisting|maintain|maintains|maintained|maintaining|"
    r"concede|concedes|conceded|conceding|acknowledge|acknowledges|acknowledged|"
    r"warn|warns|warned|warning|challenge|challenges|challenged|challenging|"
    r"deny|denies|denied|denying|refute|refutes|refuted|refuting|"
    r"according|regarding)\b",
    re.IGNORECASE,
)

# Words that introduce subordinate clauses or prepositional phrases before a
# comma ("After Bob responded, X", "In the note, X", "On Friday, X"). When
# the text before the last comma starts with one of these, the reporting verb
# in that text (if any) belongs to a subordinate clause, NOT to an attribution
# frame. This prevents false abstentions on ordinary factual context-setting.
_SUBORDINATOR = re.compile(
    r"^\s*(?:after|although|as|because|before|even|if|in|on|since|though|"
    r"unless|until|when|where|whether|while|whereas|wherever|whenever|"
    r"however|therefore|despite|although|whereupon)\b",
    re.IGNORECASE,
)


def _is_attributed_frame(sentence_text: str, match: re.Match) -> bool:
    """Return True when the verbatim claim sits inside an attribution frame
    (quoted, colon-introduced, comma-fronted, or indirect speech) instead of
    being the sentence's own assertion. See the frame notes above.

    ``match`` is the claim's regex match within ``sentence_text``. Frames 1-3
    are positional (quote abutting the claim, colon or comma before it). For
    the comma-fronted frame (3) and the indirect-speech frame (4), a reporting
    verb must be *syntactically connected* to the claim: a subordinating
    conjunction or preposition before the comma ('After Bob responded, X',
    'In the note, X') does NOT count as attribution.
    """
    start, end = match.start(), match.end()
    before = sentence_text[:start]
    after = sentence_text[end:]
    # 1. Quoted material: a quotation mark abuts the claim.
    if start > 0 and sentence_text[start - 1] in _QUOTE_CHARS:
        return True
    if end < len(sentence_text) and sentence_text[end] in _QUOTE_CHARS:
        return True
    # 2. Colon-introduced content ('the assertion: X').
    if ":" in before:
        return True
    # 3. Comma-fronted attribution: a comma precedes the claim AND a reporting
    #    verb appears in the text before the last comma. That text must NOT
    #    start with a subordinating conjunction or preposition ('After Bob
    #    responded, X', 'In the note, X', 'On Friday, X') — those are
    #    subordinate clauses carrying the claim, not attribution. Only genuine
    #    attribution openers like 'According to Alice, X' survive both checks.
    if "," in before:
        last_comma_idx = before.rfind(",")
        pre_comma = before[:last_comma_idx]
        if _REPORTING_VERB.search(pre_comma) and not _SUBORDINATOR.match(pre_comma):
            return True
    # 4. Indirect speech: a reporting verb is *immediately adjacent* to the claim
    #    ('Alice said X', 'X, said Alice'). For `before`, only check the text
    #    after the last comma (so a verb in a subordinate clause like
    #    'After Bob responded, X' is not mistaken for attribution). For `after`,
    #    only check the first meaningful token — a postposed speaker predicate
    #    ('said Alice') — so distant clauses ('and Bob responded', 'Bob replied
    #    by email') do not trigger false abstention.
    _before_adj = before.rsplit(",", 1)[-1] if "," in before else before
    _after_stripped = after.lstrip(" \t,;:." + _QUOTE_CHARS + "!?\u2014-")
    _after_tokens = _after_stripped.split()
    _after_first = _after_tokens[0] if _after_tokens else ""
    if _REPORTING_VERB.search(_before_adj) or \
            (_after_first and _REPORTING_VERB.search(_after_first)):
        return True
    return False


_CONDITIONAL = re.compile(
    r"\b(?:if|unless|whether|might|may|could|would|should|must|optional|provided|"
    r"assuming|given that|provided that|as long as)\b"
)


def _span(claim_text: str, chunk_text: str) -> tuple[int, int] | None:
    """Locate the claim text inside the chunk; ``None`` when not found.

    Matching is case-insensitive but performed on the **original** text with
    ``re.IGNORECASE`` so the returned offsets are original-source offsets.
    ``str.casefold()`` can expand characters (``ß`` -> ``ss``) and would shift
    every subsequent offset, so it must not be used to compute offsets.
    """
    if not claim_text:
        return None
    match = re.search(re.escape(claim_text), chunk_text, re.IGNORECASE)
    if match is None:
        return None
    return (match.start(), match.end())


def _citation(
    chunk_id: str,
    item: RetrievedItem,
    claim_text: str,
    grounding_score: float | None = None,
    span: tuple[int, int] | None = None,
) -> Citation:
    return Citation(
        chunk_id=chunk_id,
        document_id=item.document_id,
        filename=item.metadata.get("filename") if item.metadata else None,
        page=item.page,
        section=item.section,
        span=span if span is not None else _span(claim_text, item.content),
        workspace_id=item.workspace_id,
        grounding_score=grounding_score,
    )


@runtime_checkable
class ClaimGrounder(Protocol):
    """Decides whether a claim is substantiated by a chunk's content.

    ``score`` returns a float in ``[0, 1]`` (``1`` = fully grounded). A citation
    is accepted only when ``score(claim_text, chunk_content) >= threshold``.

    Implementations are product glue: v1 default is :class:`ExtractiveGrounder`
    (verbatim-substring check); a future semantic (embedding) grounder can
    replace it behind this same interface without touching :func:`verify_claims`.
    """

    threshold: float

    def score(self, claim_text: str, chunk_content: str) -> float:
        """Return grounding score in ``[0, 1]`` for this claim/chunk pair."""
        ...


class ExtractiveGrounder:
    """Default v1 grounder (interim, conservative, context-preserving).

    A claim is grounded **only** when its text appears **verbatim** —
    case-insensitive, contiguous, and **word-bounded** — inside a single
    sentence of the chunk, *and* that sentence **asserts** the claim as fact.
    A sentence does not assert the claim when it:

    * negates it (``"It is false that X"``, ``"X is not..."``),
    * conditionally withholds it (``"If X, notify ..."``), or
    * merely reports/contests it as attributed speech.

        Attribution is detected as four **frames**, not with an open-ended verb
    denylist. The claim is rejected when it is attributed speech:

    * enclosed in quotation marks — ``"X," said Alice.``, ``Alice said "X."``;
    * introduced by a colon — ``Alice challenged the assertion: X``,
      ``the claim: X``;
    * comma-fronted — ``According to Alice, X``; or
    * indirect speech via a reporting verb — ``Alice said X``,
      ``X, said Alice``.

    This preserves the complete attributed context (the source is quoting or
    contesting X) instead of asserting the extracted clause X as verified
    fact, which is the context-preserving quotation mode required for the
    interim. Word boundaries mean "900 dollars" cannot match inside "1900
    dollars". The sentence-context guard means a sentence that negates,
    conditionally withholds, or attributes the claim is not trusted (we still
    accept a *different*, clean sentence of the same chunk).

    Together these reject the support-vs-contradiction failures that a
    token-overlap or bare-substring scorer cannot see (negation stripping,
    entity/role swap, quantity/date changes, clause-level negation,
    conditionals, quoted/attributed speech, partial-numeric sub-values). Claims
    that cannot be verified by these checks abstain at ``INSUFFICIENT_EVIDENCE``
    rather than risk a false ``HIGH``.

    **Decimal safety.** The sentence splitter's boundary regex does *not* match
    a period directly preceded by a digit, so a number such as ``1.25`` is kept
    as a single token instead of being split into two sentences ("1" and "25").
    This prevents a spurious "25 percent." claim from matching the tail of
    "1.25 percent."

    **Original-offset spans.** ``locate`` (and the citation spans built from
    it) match on the **original** text with ``re.IGNORECASE``; they never
    search a ``casefold()`` copy, so offsets remain exact even when
    case-folding would expand a character (e.g. ``Straße`` -> ``strasse``).

    Limitations (disclosed, **not** shipped as passes):
    - Paraphrases are not validated (the claim must be a verbatim sentence
      fragment; paraphrase is a separate semantic-release gate).
    - Cross-language: a translation's text is not a substring of a source in a
      different language, so EN→RO/PT abstains — the multilingual release gate,
      not a v1 extractive limitation.
    A future semantic (embedding) grounder can replace this behind the
    unchanged :class:`ClaimGrounder` interface.
    """

    threshold: float

    def __init__(self, threshold: float = 1.0) -> None:
        self.threshold = threshold

    def score(self, claim_text: str, chunk_content: str) -> float:
        """Return 1.0 if the claim is verbatim-grounded in an unmarked sentence."""
        accepted, _ = self._find_supporting_sentence(claim_text, chunk_content)
        return 1.0 if accepted else 0.0

    def _find_supporting_sentence(
        self, claim_text: str, chunk_content: str
    ) -> tuple[bool, tuple[int, int] | None]:
        """Shared sentence-level grounding helper.

        Returns (accepted, span) where accepted is True only when the claim
        appears verbatim (case-insensitive, word-bounded, with a numeric
        boundary guard against partial decimal sub-matching like 25 in 1.25)
        inside a single sentence that *asserts* the claim: the sentence does
                not negate or condition it, and the claim is not quoted, colon-,
        comma-fronted, or indirect-speech attributed content. span is the
        (start, end) offsets
        of the claim text within chunk_content, measured in the **original**
        text (case-insensitive matching uses ``re.IGNORECASE`` on the original
        string, never ``casefold()``, so offsets stay valid even when
        case-folding would expand characters such as ``ß`` -> ``ss``).
        """
        claim_norm = claim_text.strip().rstrip(".,;:!?")
        if not claim_norm:
            return (False, None)

        # Pattern: (?<![0-9.]) prevents matching within decimal numbers
        # (e.g. 25 in 1.25); word boundaries prevent partial word matches
        # (e.g. 900 in 1900). Substring matching allows partial-sentence
        # claims like Alpha evidence in Alpha evidence about permissions.
        # re.IGNORECASE on the ORIGINAL text keeps match offsets in original
        # coordinates.
        pattern = re.compile(
            r"(?<![0-9.])\b" + re.escape(claim_norm) + r"\b", re.IGNORECASE
        )

        pos = 0
        for m in _SENTENCE_BOUNDARY.finditer(chunk_content):
            sentence_start = pos
            sentence_raw = chunk_content[sentence_start:m.start()]
            pos = m.end()
            if not sentence_raw.strip():
                continue
            match = pattern.search(sentence_raw)
            if not match:
                continue
            folded = sentence_raw.casefold()
            if (_NEGATION.search(folded)
                    or _CONDITIONAL.search(folded)
                    or _is_attributed_frame(sentence_raw, match)):
                continue
            # Offsets are relative to sentence_raw; map back to chunk_content.
            return (True, (sentence_start + match.start(),
                           sentence_start + match.end()))

        sentence_raw = chunk_content[pos:]
        if sentence_raw.strip():
            match = pattern.search(sentence_raw)
            if match:
                folded = sentence_raw.casefold()
                if not (_NEGATION.search(folded)
                        or _CONDITIONAL.search(folded)
                        or _is_attributed_frame(sentence_raw, match)):
                    return (True, (pos + match.start(), pos + match.end()))

        return (False, None)

    def locate(self, claim_text: str, chunk_content: str) -> tuple[int, int] | None:
        """Return (start, end) offsets of the claim in the accepted sentence,
        or None if the claim is not grounded."""
        accepted, span = self._find_supporting_sentence(claim_text, chunk_content)
        if accepted and span is not None:
            return span
        return None


async def verify_claims(
    claims: Sequence[Claim],
    items: Sequence[RetrievedItem],
    *,
    chunk_lookup: ChunkLookup | None = None,
    allowed_workspace_ids: set[str] | None = None,
    claim_grounder: ClaimGrounder | None = None,
) -> tuple[list[Claim], list[Citation]]:
    """Return the claims that still have at least one valid, in-scope, *grounded*
    citation.

    A ``chunk_id`` is valid only when it resolves to a chunk that **exists**, is
    **inside the caller's workspace scope**, and (when ``claim_grounder`` is set)
    **substantiates the claim**: ``grounder.score(claim.text, chunk.content) >=
    grounder.threshold``. Citations that fail grounding are dropped, and a claim
    left with no surviving citation is dropped from the output.
    Lookup-resolved citations (chunk not in the retrieved context) cannot be
    grounded — there is no chunk content to score — so they are dropped when a
    grounder is configured, keeping the verified set strictly to chunks that were
    retrieved *and* support the claim.

    When ``claim_grounder`` is ``None`` the grounding gate is skipped and the
    behaviour is existence + scope only (the legacy contract).
    """
    by_chunk = {
        item.chunk_id: item
        for item in items
        if item.chunk_id
        and item.workspace_id is not None
        and (allowed_workspace_ids is None or item.workspace_id in allowed_workspace_ids)
    }

    verified: list[Claim] = []
    citations: list[Citation] = []
    for claim in claims:
        valid_ids: list[str] = []
        for chunk_id in claim.chunk_ids:
            item = by_chunk.get(chunk_id)
            if item is not None:
                grounding_score: float | None = None
                span: tuple[int, int] | None = None
                if claim_grounder is not None:
                    grounding_score = claim_grounder.score(claim.text, item.content)
                    if grounding_score < claim_grounder.threshold:
                        # Chunk does not substantiate the claim — not a citation.
                        continue
                    # Try to locate the exact span for the citation
                    if hasattr(claim_grounder, "locate"):
                        span = claim_grounder.locate(claim.text, item.content)
                valid_ids.append(chunk_id)
                citations.append(_citation(chunk_id, item, claim.text, grounding_score, span))
            elif chunk_lookup is not None and claim_grounder is None:
                # Only existence/scope (no content to ground against).
                citation = await chunk_lookup.get(chunk_id)
                if citation is not None:
                    # Workspace invariant: lookup-resolved chunks must carry a
                    # workspace and must be inside the allowed scope (if set).
                    if citation.workspace_id is not None:
                        if (allowed_workspace_ids is None
                                or citation.workspace_id in allowed_workspace_ids):
                            valid_ids.append(chunk_id)
                            citations.append(citation)
        if valid_ids:
            verified.append(Claim(text=claim.text, chunk_ids=valid_ids))
    return verified, citations
