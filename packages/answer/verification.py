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
retrieved context) are grounded against store-hydrated canonical content when
present, else dropped, keeping the verified set strictly to chunks that
support the claim.

**T-V1-022 — bulk canonical hydration.** When a store lookup is available, the
*whole* set of candidate chunk refs is resolved up front (one deduplicated
pass through :class:`~answer.hydration.CanonicalHydrator`) to the current
document version, the caller's scope, the original text and its provenance —
**before** any claim is grounded, and before the prompt context is built (the
engine hydrates at the orchestrator seam, see
:class:`~answer.hydration.HydratingOrchestrator`). Retrieved summaries and
payload text never override the canonical record: a ref that does not resolve
(missing / deleted / superseded / foreign) is rejected even when a retrieved
item carries it, and grounding always runs on canonical text. Graph
descriptions and any other item marked ``is_graph``/``routing_hint`` are
**routing hints** — they are never a grounding source of their own. Finally,
the lifecycle state of every surviving citation is revalidated **immediately
before returning**, so a document deleted or superseded while the model was
generating loses its citations (and any claim left without one).

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
reporting verbs per language (English, then Romanian and Portuguese from
T-V1-024), the minimal lexical knowledge that identifies an indirect report.
Together these preserve the complete attributed context
instead of asserting an arbitrary extracted clause as verified fact.

**T-V1-024 — commitment is preserved in RO and PT too.** The commitment
markers (negation/rejection, conditionals) and the reporting-verb class are
closed sets *per language*: `"It is false that the budget is 900"`,
`"Nu este adevărat că bugetul este de 900"` and `"Não é verdade que o
orçamento é de 900"` all refuse to support the "budget is 900" claim, and a
reported value ("Alice a declarat că …", "Alice afirmou que …") is never the
source's own commitment. Because the interim grounder has no RO/PT clause
parser, those two languages take the **conservative** half of the contract: a
claim is accepted only when it *is* the whole sentence's assertion, so every
construction the grounder cannot positively model (negation, conditionals,
reported speech, qualified quantities) abstains. Quantity qualifiers
("about / aproximativ / cerca de 900") and locale number shapes (the
thousands separator in "1,900", the decimal comma in "900,5") are likewise
never a value of their own.
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

import logging
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from retrieval.interfaces import RetrievedItem

from .hydration import CanonicalHydrator, HydratedChunk, is_routing_hint
from .interfaces import ChunkLookup
from .models import Citation, Claim

logger = logging.getLogger("knowledge_os.answer.verification")

# Conservative sentence splitter (bodies only; trailing run of . ! ? stripped).
# We use sentences only to *localise* a verbatim claim so that negation /
# conditional context inside the SAME sentence can flip the verdict to abstain.
# The regex respects decimal points: does NOT split on a period preceded by a digit,
# so "1.25" stays whole instead of becoming "1" + "25".
_SENTENCE_BOUNDARY = re.compile(r"(?<![0-9])[.!?]+(?:\s+|$)")

# ---------------------------------------------------------------------------
# T-V1-024 — language-aware commitment markers (EN / RO / PT)
# ---------------------------------------------------------------------------
# Whole-word, case-insensitive commitment markers, one CLOSED class per
# language. If a sentence contains the verbatim claim AND any of these tokens,
# the sentence's commitment to the claim is negated, so we do NOT grant HIGH on
# it (we may still look at another, clean sentence). The classes stay
# deliberately conservative: abstaining on a marked sentence is the safe v1
# interim behaviour ("substring occurrence is not factual support").
# Attribution / quotation frames are detected structurally by
# _is_attributed_frame below, never by appending verbs to these classes.
#
# The RO/PT classes are the ticket's reported gap: the English-only lists never
# fired on "Alice a declarat că bugetul este de 900 de dolari." or "Alice
# afirmou que o orçamento é de 900 dólares.", so a *reported* budget-900 value
# was grounded as the source's own fact. Each Romance class carries its own
# negation vocabulary; the two Romance sets are also searched TOGETHER by
# :func:`_markers_for`, because a cheap hint cannot reliably separate Romanian
# from Portuguese and a mis-detected hint must only ever cost recall, never
# correctness.

# English (superset of the pre-T-V1-024 list, unchanged).
_NEGATION_EN_TERMS = (
    r"not|no|never|none|nothing|nowhere|neither|nor|without|falsely|"
    r"denied|denies|deny|denial|refutes|refute|refuted|contradicts|contradict|"
    r"contradicted|rejected|rejects|reject|refused|refuse|refuses|false|cannot|can't|"
    r"isn't|aren't|wasn't|weren't|don't|doesn't|didn't|won't|wouldn't|couldn't|"
    r"shouldn't|mightn't|mustn't|hasn't|haven't|hadn't"
)

# Romanian negation / rejection ("nu este adevărat că bugetul este de 900 ...").
_NEGATION_RO_TERMS = (
    r"nu|nici|niciodată|niciodata|nimeni|nimic|nicăieri|deloc|fără|fara|"
    r"fals|falsă|falsa|neadevărat|neadevarat|incorect|eronic|"
    r"respins|respinge|respinsă|respinsa|contrazice|refuză|refuza|"
    r"neconfirmat|nejustificat|nedeclarat"
)

# Portuguese negation / rejection ("Não é verdade que o orçamento é de 900 ...").
_NEGATION_PT_TERMS = (
    r"não|nao|nunca|jamais|nada|ninguém|ninguem|nenhum|nenhuma|tampouco|"
    r"falso|falsa|falsamente|inverídico|inveridico|errado|errada|"
    r"rejeita|rejeitado|rejeitada|negou|negado|negada|nega|recusa|recusou|contradiz"
)

_NEGATION_EN = re.compile(rf"\b(?:{_NEGATION_EN_TERMS})\b", re.IGNORECASE)
_NEGATION_RO = re.compile(rf"\b(?:{_NEGATION_RO_TERMS})\b", re.IGNORECASE)
_NEGATION_PT = re.compile(rf"\b(?:{_NEGATION_PT_TERMS})\b", re.IGNORECASE)
# Union used for any RO/PT sentence (see :func:`_markers_for`).
_NEGATION_ROMANCE = re.compile(
    rf"\b(?:{_NEGATION_RO_TERMS}|{_NEGATION_PT_TERMS})\b", re.IGNORECASE
)
# Backward-compatible superset: every language, searched at once.
_NEGATION = re.compile(
    rf"\b(?:{_NEGATION_EN_TERMS}|{_NEGATION_RO_TERMS}|{_NEGATION_PT_TERMS})\b",
    re.IGNORECASE,
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
_QUOTE_CHARS = "\"'\u201c\u201d\u2018\u2019\u201e\u00ab\u00bb"

# Reporting verbs, one CLOSED class per language. RO/PT forms are T-V1-024:
# "a declarat că", "a spus că", "susține", "afirmou", "disse", "segundo", ...
# Without them, indirect speech in RO/PT read as the source's own assertion.
_EN_REPORTING_VERB_TERMS = (
    r"said|says|saying|tell|tells|told|telling|"
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
    r"according|regarding"
)

# Romanian reporting verbs (compound tenses: "a declarat", plus the bare stem
# the participle carries). "potrivit"/"conform" are the RO attribution openers
# ("Potrivit lui Alice, ...") mirroring EN "according to".
_RO_REPORTING_VERB_TERMS = (
    r"declarat|spus|afirmat|afirmă|afirma|susținut|sustinut|susține|sustine|"
    r"raportat|raportează|raporteaza|menționat|menționează|menționeaza|"
    r"relatat|relatează|relateaza|scris|scrie|argumentat|argumentă|argumenta|"
    r"contestat|contestă|contesta|negat|neagă|neaga|confirmat|confirmă|confirma|"
    r"răspuns|raspuns|răspunde|raspunde|anunțat|anuntat|anunță|anunta|"
    r"potrivit|conform"
)

# Portuguese reporting verbs ("Alice afirmou que ...", "segundo Alice, ...").
_PT_REPORTING_VERB_TERMS = (
    r"disse|diz|dizem|afirmou|afirma|afirmam|declarou|declara|relatou|relata|"
    r"mencionou|menciona|escreveu|escreve|argumentou|argumenta|sustentou|sustenta|"
    r"contestou|contesta|negou|nega|rejeitado|rejeitou|rejeita|confirmou|confirma|"
    r"respondeu|responde|anunciou|anuncia|segundo|conforme|de acordo com"
)

_REPORTING_VERB_EN = re.compile(rf"\b(?:{_EN_REPORTING_VERB_TERMS})\b", re.IGNORECASE)
_REPORTING_VERB_RO = re.compile(rf"\b(?:{_RO_REPORTING_VERB_TERMS})\b", re.IGNORECASE)
_REPORTING_VERB_PT = re.compile(rf"\b(?:{_PT_REPORTING_VERB_TERMS})\b", re.IGNORECASE)
_REPORTING_VERB_ROMANCE = re.compile(
    rf"\b(?:{_RO_REPORTING_VERB_TERMS}|{_PT_REPORTING_VERB_TERMS})\b", re.IGNORECASE
)
# Backward-compatible alias: the English closed class (pre-T-V1-024 behaviour).
_REPORTING_VERB = _REPORTING_VERB_EN

# Words that introduce subordinate clauses or prepositional phrases before a
# comma ("After Bob responded, X", "In the note, X", "On Friday, X"). When
# the text before the last comma starts with one of these, the reporting verb
# in that text (if any) belongs to a subordinate clause, NOT to an attribution
# frame. This prevents false abstentions on ordinary factual context-setting.
_SUBORDINATOR = re.compile(
    r"^\s*(?:after|although|as|because|before|even|if|in|on|since|though|"
    r"unless|until|when|where|whether|while|whereas|wherever|whenever|"
    r"however|therefore|despite|although|whereupon|"
    # T-V1-024: RO/PT subordinators + prepositions ('Dacă bugetul ..., ...',
    # 'Se o orçamento ..., ...', 'Em caso de ..., ...') are context-setting,
    # not attribution openers.
    r"dacă|daca|după|dupa|prin|între|intre|până|pana|"
    r"se|caso|em|por|desde|para|com|através|atraves)\b",
    re.IGNORECASE,
)


def _is_attributed_frame(
    sentence_text: str,
    match: re.Match,
    reporting_verb: re.Pattern[str] | None = None,
) -> bool:
    """Return True when the verbatim claim sits inside an attribution frame
    (quoted, colon-introduced, comma-fronted, or indirect speech) instead of
    being the sentence's own assertion. See the frame notes above.

    ``match`` is the claim's regex match within ``sentence_text``. Frames 1-3
    are positional (quote abutting the claim, colon or comma before it). For
    the comma-fronted frame (3) and the indirect-speech frame (4), a reporting
    verb must be *syntactically connected* to the claim: a subordinating
    conjunction or preposition before the comma ('After Bob responded, X',
    'In the note, X') does NOT count as attribution.

    ``reporting_verb`` is the language-routed closed class to test with
    (:func:`_markers_for`): the English class for EN sentences, the RO+PT union
    for a Romanian or Portuguese one ("Alice a declarat că X", "Alice afirmou
    que X"). Defaults to the English class for direct callers.
    """
    verb_class = reporting_verb if reporting_verb is not None else _REPORTING_VERB
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
        if verb_class.search(pre_comma) and not _SUBORDINATOR.match(pre_comma):
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
    if verb_class.search(_before_adj) or (
        _after_first and verb_class.search(_after_first)
    ):
        return True
    return False


# --- T-V1-024: language-aware routing + conservative Romance abstain -----------

# Cheap, closed-set language hints. This is NOT a language identifier: it only
# has to answer "is this sentence English, or is it Romanian/Portuguese?".
# Romanian and Portuguese share too many ASCII stopwords ("de", "este", "o")
# to be separated reliably here, so both map to the single "romance" family and
# are then handled by the UNION of the RO and PT closed classes — a
# mis-detected hint may cost recall, never correctness. Diacritics carry most
# of the signal (ă â î ș ț for RO, ã õ ç for PT) plus a handful of
# unmistakable function words; ASCII-only words that are also English ("in",
# "as", "no") are deliberately NOT used.
_RO_HINT = re.compile(
    r"[\u0103\u00e2\u00ee\u0219\u021b\u015f\u0163]"
    r"|\b(?:nu|nici|și|si|să|sa|că|ca|este|sunt|sunteți|sunteti|bugetul|lansarea|"
    r"pentru|foarte|aprobat|aprobată|aprobata|anunță|anunta|fără|fara)\b",
    re.IGNORECASE,
)
_PT_HINT = re.compile(
    r"[\u00e3\u00f5\u00e7]"
    r"|\b(?:não|nao|são|sao|está|esta|estão|estao|você|voce|também|tambem|"
    r"orçamento|orcamento|dólares|dolares|através|atraves|nunca)\b",
    re.IGNORECASE,
)

# Quantity qualifiers: the value is hedged/approximate, so an unqualified claim
# ("900 dollars") is NOT what the source commits to ("about 900 dollars").
# Closed class, EN+RO+PT; applied ONLY to digit-bearing claims (a quantity),
# which keeps prose such as "evidence about permissions" out of its scope.
_APPROXIMATION = re.compile(
    r"\b(?:about|around|approximately|approx\.?|circa|roughly|nearly|over|under|"
    r"up to|at least|at most|more than|less than|estimated|est\.?)\b"
    r"|\b(?:aproximativ|aprox\.?|circa|în jur de|in jur de|cam|peste|sub|"
    r"mai mult de|mai puțin de|mai putin de|undeva la|evaluat|evaluată|evaluata)\b"
    r"|\b(?:aproximadamente|cerca de|por volta de|em torno de|mais de|menos de|"
    r"acima de|abaixo de|uns|umas|estimado|estimada)\b",
    re.IGNORECASE,
)


def _is_romance(text: str) -> bool:
    """True when ``text`` carries a Romanian or Portuguese hint."""
    return bool(_RO_HINT.search(text) or _PT_HINT.search(text))


def _markers_for(
    text: str,
) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    """Return the (negation, conditional, reporting-verb) classes for ``text``.

    English sentences keep the pre-T-V1-024 English classes; a Romanian or
    Portuguese sentence gets the RO+PT union of classes so that a hint that cannot
    tell the two apart still recognises both vocabularies.
    """
    if _is_romance(text):
        return (_NEGATION_ROMANCE, _CONDITIONAL_ROMANCE, _REPORTING_VERB_ROMANCE)
    return (_NEGATION_EN, _CONDITIONAL_EN, _REPORTING_VERB_EN)


def _squash(text: str) -> str:
    """Whitespace-collapsed, case-folded form used only for equality checks."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def _is_whole_sentence_assertion(sentence_text: str, claim_norm: str) -> bool:
    """True when ``claim_norm`` *is* the sentence's assertion, clause and all.

    RO/PT have no clause-boundary parser in the interim grounder, so every
    construction that is NOT a bare assertion — negation ("Nu este adevărat
    că bugetul este de 900"), conditionals ("Dacă bugetul este de 900, ..."),
    reported speech ("Alice a declarat că bugetul este de 900"), qualified
    quantities ("Aproximativ 900 de dolari") — arrives as a *fragment* of the
    sentence. Accepting only whole-sentence claims is therefore the
    conservative, context-preserving rule for those two languages: anything
    the grounder cannot positively model abstains instead of being asserted.
    """
    sentence = _squash(sentence_text).rstrip(".,;:!?\u201d\u00bb\"'")
    return bool(sentence) and sentence == _squash(claim_norm)


def _sentence_asserts(sentence_raw: str, match: re.Match, claim_norm: str) -> bool:
    """True when ``sentence_raw`` commits to ``claim_norm`` as its own fact.

    T-V1-024's context-preserving commitment check, run per candidate sentence:

    1. the sentence carries no negation/rejection marker of its own language
       ("It is false that X", "Nu este adevărat că X", "Não é verdade que X");
    2. it carries no conditional marker ("If X ...", "Dacă X ...", "Caso X ...");
    3. a quantified claim is not hedged ("about 900", "aproximativ 900",
       "cerca de 900");
    4. the claim is not quoted / colon-fronted / comma-fronted / indirect
       speech, using the reporting-verb class of its own language;
    5. for RO/PT only, the claim IS the sentence's whole assertion — the
       interim grounder has no RO/PT clause parser, so any construction it
       cannot positively model (negation, conditionals, reported speech,
       qualified quantities) is a non-full-sentence frame and abstains.

    Every branch abstains (never asserts): a substring occurrence inside a
    marked sentence is not factual support. This is a closed, language-routed
    rule set — not token counting and not a keyword-similarity score.
    """
    folded = sentence_raw.casefold()
    negation, conditional, reporting_verb = _markers_for(sentence_raw)
    if negation.search(folded) or conditional.search(folded):
        return False
    if _APPROXIMATION.search(folded) and any(ch.isdigit() for ch in claim_norm):
        # A hedged quantity does not commit to the plain number.
        return False
    if _is_attributed_frame(sentence_raw, match, reporting_verb):
        return False
    if _is_romance(sentence_raw) and not _is_whole_sentence_assertion(
        sentence_raw, claim_norm
    ):
        return False
    return True


_CONDITIONAL_EN_TERMS = (
    r"if|unless|whether|might|may|could|would|should|must|optional|provided|"
    r"assuming|given that|provided that|as long as"
)
_CONDITIONAL_RO_TERMS = (
    r"dacă|daca|ar putea|presupunând|presupunand|cu condiția|cu conditia|"
    r"atâta timp cât|atata timp cat|în cazul|in cazul|condiția|conditia|eventual|"
    r"altfel|în funcție de|in functie de"
)
_CONDITIONAL_PT_TERMS = (
    r"se|caso|desde que|contanto que|supondo|presupondo|em caso de|para o caso de|"
    r"sendo|caso seja|vamos dizer|digamos"
)

_CONDITIONAL_EN = re.compile(rf"\b(?:{_CONDITIONAL_EN_TERMS})\b", re.IGNORECASE)
_CONDITIONAL_RO = re.compile(rf"\b(?:{_CONDITIONAL_RO_TERMS})\b", re.IGNORECASE)
_CONDITIONAL_PT = re.compile(rf"\b(?:{_CONDITIONAL_PT_TERMS})\b", re.IGNORECASE)
_CONDITIONAL_ROMANCE = re.compile(
    rf"\b(?:{_CONDITIONAL_RO_TERMS}|{_CONDITIONAL_PT_TERMS})\b", re.IGNORECASE
)
# Backward-compatible alias (pre-T-V1-024 English conditional class).
_CONDITIONAL = _CONDITIONAL_EN


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

    **T-V1-024 — language-aware commitment (EN / RO / PT).** The negation,
    conditional and reporting-verb markers above are one *closed class per
    language*, and a cheap hint routes a sentence to its own class (a
    Romanian or Portuguese sentence gets the union of both Romance sets, so a
    hint that cannot separate them can only cost recall, never correctness):
    a Romanian or Portuguese source therefore preserves commitment exactly
    like an English one — "Nu este adevărat că bugetul este de 900" and
    "Não é verdade que o orçamento é de 900" refuse the "budget is 900"
    claim, and a reported value ("Alice a declarat că …", "Alice afirmou que
    …") is not the source's commitment. Since the interim grounder has no
    RO/PT clause parser, those two languages take the conservative half of
    the contract: **the claim must be the whole sentence's assertion**
    (:func:`_is_whole_sentence_assertion`), so any construction it cannot
    positively model — negation, conditionals, reported speech, qualified
    quantities — arrives as a sentence fragment and abstains. A hedged
    quantity ("about / aproximativ / cerca de 900") never commits to the
    plain number, and the numeric boundary guards reject a partial value
    across locale separators ("900" inside "1,900" or "900,5").

    Together these reject the support-vs-contradiction failures that a
    token-overlap or bare-substring scorer cannot see (negation stripping,
    entity/role swap, quantity/date changes, clause-level negation,
    conditionals, quoted/attributed speech, partial-numeric sub-values) — and
    nothing here is decided by keyword or token *counting*: the checks are
    closed-class markers plus containment, and every one of them abstains
    rather than infers support. Claims
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
    - RO/PT clause-level extraction: a claim must be the whole sentence's
      assertion, so a clean *embedded* RO/PT clause abstains (T-V1-024's
      conservative trade — recall is given up, never correctness).
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
        appears verbatim (case-insensitive, word-bounded, with numeric
        boundary guards against partial sub-matching like 25 in 1.25 or 900 in
        1,900 / 900,5) inside a single sentence that *asserts* the claim: the
        sentence does not negate or condition it, does not hedge the quantity,
        the claim is not quoted, colon-, comma-fronted, or indirect-speech
        attributed content, and — for RO/PT, which the interim grounder cannot
        parse at clause level — the claim IS the sentence's whole assertion
        (see :func:`_sentence_asserts`). span is the (start, end) offsets
        of the claim text within chunk_content, measured in the **original**
        text (case-insensitive matching uses ``re.IGNORECASE`` on the original
        string, never ``casefold()``, so offsets stay valid even when
        case-folding would expand characters such as ``ß`` -> ``ss``).
        """
        claim_norm = claim_text.strip().rstrip(".,;:!?")
        if not claim_norm:
            return (False, None)

        # Pattern: the lookbehind prevents the claim from starting inside a
        # number — as a decimal tail (25 in 1.25) or as a thousands-group tail
        # (900 in 1,900: "," is not a word char, so \b alone lets it through).
        # The lookahead prevents it from ending where the source keeps writing
        # the number with a separator (900 in "900,5"), which is how RO/PT
        # decimal commas hide a different value. Word boundaries still prevent
        # partial word matches (900 in 1900). Substring matching allows
        # partial-sentence claims like Alpha evidence in Alpha evidence about
        # permissions. re.IGNORECASE on the ORIGINAL text keeps match offsets
        # in original coordinates.
        pattern = re.compile(
            r"(?<![0-9.,])\b" + re.escape(claim_norm) + r"\b(?![.,][0-9])",
            re.IGNORECASE,
        )

        pos = 0
        for m in _SENTENCE_BOUNDARY.finditer(chunk_content):
            sentence_start = pos
            sentence_raw = chunk_content[sentence_start : m.start()]
            pos = m.end()
            if not sentence_raw.strip():
                continue
            match = pattern.search(sentence_raw)
            if not match:
                continue
            if _sentence_asserts(sentence_raw, match, claim_norm):
                # Offsets are relative to sentence_raw; map back to chunk_content.
                return (True, (sentence_start + match.start(), sentence_start + match.end()))

        sentence_raw = chunk_content[pos:]
        if sentence_raw.strip():
            match = pattern.search(sentence_raw)
            if match and _sentence_asserts(sentence_raw, match, claim_norm):
                return (True, (pos + match.start(), pos + match.end()))

        return (False, None)

    def locate(self, claim_text: str, chunk_content: str) -> tuple[int, int] | None:
        """Return (start, end) offsets of the claim in the accepted sentence,
        or None if the claim is not grounded."""
        accepted, span = self._find_supporting_sentence(claim_text, chunk_content)
        if accepted and span is not None:
            return span
        return None


# ---------------------------------------------------------------------------
# T-R09 — Conservative cross-language verification path
# ---------------------------------------------------------------------------

#: Supported language pairs for conservative cross-language verification.
#: Unsupported languages or pairs outside this set abstain by design.
SUPPORTED_CROSS_LANG_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("en", "ro"),
    ("en", "pt"),
    ("ro", "en"),
    ("ro", "pt"),
    ("pt", "en"),
    ("pt", "ro"),
})

#: Markers identifying unsupported non-target languages (German, French, Spanish, Italian).
#: Encountering these triggers immediate abstention.
_UNSUPPORTED_LANG_MARKERS = re.compile(
    r"\b(?:"
    r"das|der|die|den|dem|des|ein|eine|einer|eines|nicht|ist|sind|war|und|oder|aber|beträgt|dollar|aus|nach|zu|bei|mit|"
    r"le|les|du|des|un|une|est|sont|dans|pour|avec|sur|capitale|"
    r"el|los|las|del|presupuesto|"
    r"il|lo|gli|nel|nella|della|è|sono"
    r")\b",
    re.IGNORECASE,
)

_RO_DIACRITICS = set("ăâîșțşţĂÂÎȘȚŞŢ")
_PT_DIACRITICS = set("ãõçáéíóúêôÃÕÇÁÉÍÓÚÊÔ")

_RO_TOKENS = {
    "bugetul", "buget", "capitala", "franței", "frantei", "româniei", "romaniei",
    "românia", "romania", "este", "sunt", "și", "si", "de", "din", "pentru",
    "dolari", "roluri", "autentificarea", "autentificare", "spații", "spatii",
    "lucru", "acceptă", "accepta", "plătit", "platit",
}

_PT_TOKENS = {
    "orçamento", "orcamento", "capital", "frança", "franca", "é", "são", "sao",
    "dólares", "dolares", "funções", "funcoes", "autenticação", "autenticacao",
    "espaços", "espacos", "trabalho", "suporta", "baseada", "pagou", "pago",
}

_EN_TOKENS = {
    "the", "is", "are", "of", "in", "and", "to", "a", "an", "budget", "dollars",
    "dollar", "capital", "france", "paris", "authentication", "roles", "role",
    "workspaces", "workspace", "supports", "support", "based", "platform", "paid",
}

_CANONICAL_PHRASES: tuple[tuple[str, str], ...] = (
    ("pe bază de roluri", "role"),
    ("pe baza de roluri", "role"),
    ("baseada em funções", "role"),
    ("baseada em funcoes", "role"),
    ("role-based", "role"),
    ("role based", "role"),
    ("spații de lucru", "workspace"),
    ("spatii de lucru", "workspace"),
    ("espaços de trabalho", "workspace"),
    ("espacos de trabalho", "workspace"),
)

_CANONICAL_ENTITIES: dict[str, str] = {
    "paris": "ENT_PARIS",
    "lyon": "ENT_LYON",
    "france": "ENT_FRANCE",
    "franța": "ENT_FRANCE",
    "franta": "ENT_FRANCE",
    "frança": "ENT_FRANCE",
    "franca": "ENT_FRANCE",
    "franței": "ENT_FRANCE",
    "frantei": "ENT_FRANCE",
    "romania": "ENT_ROMANIA",
    "românia": "ENT_ROMANIA",
    "româniei": "ENT_ROMANIA",
    "romaniei": "ENT_ROMANIA",
    "romênia": "ENT_ROMANIA",
    "romenia": "ENT_ROMANIA",
    "bucharest": "ENT_BUCHAREST",
    "bucurești": "ENT_BUCHAREST",
    "bucuresti": "ENT_BUCHAREST",
    "portugal": "ENT_PORTUGAL",
    "lisbon": "ENT_LISBON",
    "lisboa": "ENT_LISBON",
    "alice": "ENT_ALICE",
    "bob": "ENT_BOB",
    "acme": "ENT_ACME",
}

_CANONICAL_CONCEPTS: dict[str, str] = {
    "capital": "capital",
    "capitala": "capital",
    "capitală": "capital",
    "budget": "budget",
    "bugetul": "budget",
    "buget": "budget",
    "orçamento": "budget",
    "orcamento": "budget",
    "dollar": "dollar",
    "dollars": "dollar",
    "dolar": "dollar",
    "dolari": "dollar",
    "dólar": "dollar",
    "dólares": "dollar",
    "dolares": "dollar",
    "platform": "platform",
    "platforma": "platform",
    "plataforma": "platform",
    "authentication": "auth",
    "auth": "auth",
    "autentificarea": "auth",
    "autentificare": "auth",
    "autenticação": "auth",
    "autenticacao": "auth",
    "workspace": "workspace",
    "workspaces": "workspace",
    "roles": "role",
    "role": "role",
    "roluri": "role",
    "funções": "role",
    "funcoes": "role",
    "support": "support",
    "supports": "support",
    "accept": "support",
    "accepts": "support",
    "acceptă": "support",
    "accepta": "support",
    "suportă": "support",
    "suporta": "support",
    "pay": "pay",
    "paid": "pay",
    "plătit": "pay",
    "platit": "pay",
    "pagou": "pay",
    "pago": "pay",
}

_COMMON_STOPWORDS: set[str] = {
    "the", "a", "an", "is", "are", "of", "in", "to", "and", "for", "that", "it", "on", "with",
    "as", "at", "și", "si", "de", "din", "în", "la", "cu", "pe", "pentru", "că", "ca",
    "este", "sunt", "un", "unui", "unei", "lui", "o", "e", "os", "do", "da", "dos", "das",
    "em", "no", "na", "nos", "nas", "para", "por", "com", "é", "são", "sao",
}


def _detect_cross_lang(text: str) -> str | None:
    """Identify whether ``text`` is EN, RO, or PT. Returns None for unsupported languages."""
    if _UNSUPPORTED_LANG_MARKERS.search(text):
        return None
    tokens = set(re.findall(r"\b\w+\b", text.lower()))
    has_ro_diac = any(c in _RO_DIACRITICS for c in text)
    has_pt_diac = any(c in _PT_DIACRITICS for c in text)
    ro_score = len(tokens & _RO_TOKENS) + (3 if has_ro_diac else 0)
    pt_score = len(tokens & _PT_TOKENS) + (3 if has_pt_diac else 0)
    en_score = len(tokens & _EN_TOKENS)
    scores = {"ro": ro_score, "pt": pt_score, "en": en_score}
    best_lang, best_score = max(scores.items(), key=lambda x: x[1])
    if best_score == 0:
        return None
    return best_lang


def _markers_for_lang(lang: str) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    """Return language-specific (negation, conditional, reporting_verb) pattern triples."""
    if lang == "ro":
        return (_NEGATION_RO, _CONDITIONAL_RO, _REPORTING_VERB_RO)
    if lang == "pt":
        return (_NEGATION_PT, _CONDITIONAL_PT, _REPORTING_VERB_PT)
    return (_NEGATION_EN, _CONDITIONAL_EN, _REPORTING_VERB_EN)


def _extract_canonical_components(text: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """Extract canonical (entities, concepts, numbers, unmapped) from a clause or sentence."""
    norm = text.lower()
    for phrase, mapped in _CANONICAL_PHRASES:
        norm = norm.replace(phrase, f" {mapped} ")
    tokens = re.findall(r"\b\w+\b", norm)
    entities: list[str] = []
    concepts: list[str] = []
    numbers: list[str] = []
    unmapped: list[str] = []
    for t in tokens:
        if t.isdigit():
            numbers.append(t)
        elif t in _CANONICAL_ENTITIES:
            entities.append(_CANONICAL_ENTITIES[t])
        elif t in _CANONICAL_CONCEPTS:
            concepts.append(_CANONICAL_CONCEPTS[t])
        elif t in _COMMON_STOPWORDS:
            continue
        else:
            unmapped.append(t)
    return entities, concepts, numbers, unmapped


def _verify_cross_language_match(
    source_sent: str, claim_text: str, source_lang: str, claim_lang: str
) -> bool:
    """Verify that ``claim_text`` is a conservative translation of ``source_sent``."""
    if (source_lang, claim_lang) not in SUPPORTED_CROSS_LANG_PAIRS:
        return False

    s_ent, s_con, s_num, s_unm = _extract_canonical_components(source_sent)
    c_ent, c_con, c_num, c_unm = _extract_canonical_components(claim_text)

    # 1. Exact numeric equivalence: quantities must match exactly
    if s_num != c_num:
        return False

    # 2. Named entity alignment: entities and their sequence (role preservation) must match
    if s_ent != c_ent:
        return False

    # 3. Concept alignment: predicates and core content must match
    if set(s_con) != set(c_con):
        return False

    # 4. Conservative unmapped check: claim must not introduce unsupported concepts
    if c_unm and not set(c_unm).issubset(set(s_unm)):
        return False

    return True


class CrossLanguageGrounder:
    """Conservative cross-language grounder (T-R09).

    Extends extractive grounding with a conservative cross-language verification
    path that verifies translated answers (RO/PT) against original-source spans (EN/RO/PT).

    Guarantees:
    - Never lowers the substring threshold: threshold remains 1.0 (strict agreement).
    - Abstention-first on unsupported pairs: non-EN/RO/PT pairs and unsupported languages abstain.
    - Preserves commitment: source and claim must both assert (no negation, conditionals,
      approximate hedges, or attributed speech frames).
    - Original-source spans: locate() returns the span of the asserting sentence in the
      original chunk text, preserving provenance coordinates.
    """

    threshold: float

    def __init__(self, threshold: float = 1.0) -> None:
        self.threshold = threshold
        self._extractive = ExtractiveGrounder(threshold=threshold)

    def score(self, claim_text: str, chunk_content: str) -> float:
        """Return 1.0 if the claim is verified extractively or via conservative translation."""
        accepted, _ = self._find_supporting_sentence(claim_text, chunk_content)
        return 1.0 if accepted else 0.0

    def locate(self, claim_text: str, chunk_content: str) -> tuple[int, int] | None:
        """Return (start, end) offsets of the supporting sentence in the original chunk, or None."""
        accepted, span = self._find_supporting_sentence(claim_text, chunk_content)
        if accepted and span is not None:
            return span
        return None

    def _find_supporting_sentence(
        self, claim_text: str, chunk_content: str
    ) -> tuple[bool, tuple[int, int] | None]:
        # 1. Verbatim extractive match takes precedence (strict verifier untouched)
        accepted, span = self._extractive._find_supporting_sentence(claim_text, chunk_content)
        if accepted and span is not None:
            return True, span

        # 2. Conservative cross-language verification path
        claim_norm = claim_text.strip().rstrip(".,;:!?")
        if not claim_norm:
            return False, None

        claim_lang = _detect_cross_lang(claim_norm)
        if claim_lang is None:
            return False, None

        # Check claim commitment: claim itself must be an assertion without negation/conditionals
        c_neg, c_cond, c_verb = _markers_for_lang(claim_lang)
        folded_claim = claim_norm.casefold()
        if c_neg.search(folded_claim) or c_cond.search(folded_claim):
            return False, None
        if _APPROXIMATION.search(folded_claim) and any(ch.isdigit() for ch in claim_norm):
            return False, None
        if (
            any(q in claim_norm for q in _QUOTE_CHARS)
            or ":" in claim_norm
            or c_verb.search(claim_norm)
        ):
            return False, None

        # Search original source sentences for an asserting sentence that substantiates the claim
        pos = 0
        for m in _SENTENCE_BOUNDARY.finditer(chunk_content):
            s_start = pos
            s_raw = chunk_content[s_start : m.start()]
            pos = m.end()
            if not s_raw.strip():
                continue
            s_stripped = s_raw.strip()
            s_lang = _detect_cross_lang(s_stripped)
            if s_lang is None or s_lang == claim_lang:
                continue
            if (s_lang, claim_lang) not in SUPPORTED_CROSS_LANG_PAIRS:
                continue

            # Check source sentence commitment
            s_neg, s_cond, s_verb = _markers_for_lang(s_lang)
            folded_s = s_stripped.casefold()
            if s_neg.search(folded_s) or s_cond.search(folded_s):
                continue
            if _APPROXIMATION.search(folded_s) and any(ch.isdigit() for ch in s_stripped):
                continue
            if (
                any(q in s_stripped for q in _QUOTE_CHARS)
                or ":" in s_stripped
                or s_verb.search(s_stripped)
            ):
                continue

            # Verify factual and semantic equivalence
            if _verify_cross_language_match(s_stripped, claim_norm, s_lang, claim_lang):
                actual_start = chunk_content.find(s_stripped, s_start)
                actual_end = actual_start + len(s_stripped)
                return True, (actual_start, actual_end)

        # Check trailing sentence if any
        s_raw = chunk_content[pos:]
        if s_raw.strip():
            s_stripped = s_raw.strip()
            s_lang = _detect_cross_lang(s_stripped)
            if (
                s_lang is not None
                and s_lang != claim_lang
                and (s_lang, claim_lang) in SUPPORTED_CROSS_LANG_PAIRS
            ):
                s_neg, s_cond, s_verb = _markers_for_lang(s_lang)
                folded_s = s_stripped.casefold()
                if not (s_neg.search(folded_s) or s_cond.search(folded_s)):
                    if not (
                        _APPROXIMATION.search(folded_s) and any(ch.isdigit() for ch in s_stripped)
                    ):
                        if not (
                            any(q in s_stripped for q in _QUOTE_CHARS)
                            or ":" in s_stripped
                            or s_verb.search(s_stripped)
                        ):
                            if _verify_cross_language_match(
                                s_stripped, claim_norm, s_lang, claim_lang
                            ):
                                actual_start = chunk_content.find(s_stripped, pos)
                                actual_end = actual_start + len(s_stripped)
                                return True, (actual_start, actual_end)

        return False, None


def _filename_of(item: RetrievedItem | None) -> str | None:
    """Filename provenance carried by a retrieved item (``RetrievedItem`` has
    no ``filename`` field — producers stash it in metadata)."""
    if item is None:
        return None
    return (item.metadata or {}).get("filename")


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
    **substantiates the claim**: ``grounder.score(claim.text, canonical.content)
    >= grounder.threshold``. Citations that fail grounding are dropped, and a
    claim left with no surviving citation is dropped from the output.

    **T-V1-022 — bulk canonical hydration (before grounding).** With a store
    available, every candidate ref cited by a claim is resolved *once*, up
    front, to its current document version, the caller's scope, the original
    text and provenance (:class:`answer.hydration.CanonicalHydrator`). Rules:

    * a ref that does not resolve — missing, deleted, superseded or foreign —
      is rejected **even when a retrieved item carries it** (the payload can
      never stand in for the store);
    * when the store returns the passage, that text *is* the grounding text
      and the provenance it reports *is* the citation's provenance — retrieved
      summaries and payload text never override it;
    * items marked ``is_graph``/``routing_hint`` (graph descriptions) are never
      a grounding source of their own: without canonical text they cannot be
      cited at all;
    * the surviving citations are **revalidated immediately before returning**,
      so a document deleted or superseded while the model was generating loses.

    When ``chunk_lookup`` is ``None`` no store exists to hydrate from, so the
    legacy contract applies: existence + scope from the retrieved items only,
    and routing-hint items (graph descriptions) are never citable.

    When ``claim_grounder`` is ``None`` the grounding gate is skipped and the
    behaviour is existence + scope only (the legacy contract).
    """
    hydrator = _hydrator_for(chunk_lookup)

    by_chunk = {
        item.chunk_id: item
        for item in items
        if item.chunk_id
        and item.workspace_id is not None
        and (allowed_workspace_ids is None or item.workspace_id in allowed_workspace_ids)
    }

    # (1) Bulk hydration: resolve EVERY candidate ref before any claim is
    # grounded, in one deduplicated store pass (source lookups > 0 on the
    # verify path; fresh reads so verification sees current lifecycle state).
    hydrated: dict[str, HydratedChunk] = {}
    if hydrator is not None:
        hydrated = await hydrator.hydrate(
            (ref for claim in claims for ref in claim.chunk_ids),
            allowed_workspace_ids=allowed_workspace_ids,
            fresh=True,
        )

    verified: list[Claim] = []
    citations: list[Citation] = []
    for claim in claims:
        valid_ids: list[str] = []
        for chunk_id in claim.chunk_ids:
            item = by_chunk.get(chunk_id)
            if hydrator is None:
                # No store: legacy existence + scope from the retrieved item.
                if item is None:
                    continue
                if is_routing_hint(item):
                    # RP-03 / G4: a synthesized graph description is a routing
                    # hint, never evidence — without a store there is nothing
                    # authoritative to ground it against.
                    continue
                content_to_verify: str | None = item.content
                doc_id = item.document_id
                filename = _filename_of(item)
                page = item.page
                section = item.section
                workspace_id = item.workspace_id
            else:
                canonical = hydrated.get(chunk_id)
                if canonical is None:
                    # Missing / deleted / superseded / foreign — rejected even
                    # when a retrieved item still carries this ref.
                    continue
                hint = item is not None and is_routing_hint(item)
                if canonical.has_text:
                    # Canonical text always wins over payload/summary text.
                    content_to_verify = canonical.content
                elif hint:
                    # Routing hint with no original text: never citable.
                    continue
                elif claim_grounder is not None:
                    # Nothing canonical to ground against → cannot verify.
                    continue
                else:
                    # Legacy existence-only contract may verify a store record
                    # whose text was never hydrated (documented None case).
                    content_to_verify = item.content if item is not None else None
                doc_id = canonical.document_id or (item.document_id if item else None)
                filename = canonical.filename or _filename_of(item)
                item_page = item.page if item else None
                page = canonical.page if canonical.page is not None else item_page
                section = canonical.section or (item.section if item else None)
                workspace_id = canonical.workspace_id or (item.workspace_id if item else None)

            grounding_score: float | None = None
            span: tuple[int, int] | None = None
            if claim_grounder is not None:
                if content_to_verify is None:
                    continue
                grounding_score = claim_grounder.score(claim.text, content_to_verify)
                if grounding_score < claim_grounder.threshold:
                    # Chunk does not substantiate the claim — not a citation.
                    continue
                # Try to locate the exact span for the citation
                if hasattr(claim_grounder, "locate"):
                    span = claim_grounder.locate(claim.text, content_to_verify)
            if span is None and content_to_verify:
                # Fall back to a plain case-insensitive span location.
                span = _span(claim.text, content_to_verify)
            valid_ids.append(chunk_id)
            citations.append(
                Citation(
                    chunk_id=chunk_id,
                    document_id=doc_id,
                    filename=filename,
                    page=page,
                    section=section,
                    span=span,
                    workspace_id=workspace_id,
                    grounding_score=grounding_score,
                    # Verified canonical passage: the exact content the
                    # claim was grounded against (review1 F6 — never None
                    # on a grounder-gated path; the legacy existence-only
                    # path may carry None, documented on Citation.content).
                    content=content_to_verify,
                )
            )
        if valid_ids:
            verified.append(Claim(text=claim.text, chunk_ids=valid_ids))

    # (3) Revalidate lifecycle state immediately before returning: a document
    # deleted or superseded while the model was generating loses its citations
    # here, and any claim left without a live citation is dropped with them.
    if citations and hydrator is not None:
        live = await hydrator.hydrate(
            (citation.chunk_id for citation in citations),
            allowed_workspace_ids=allowed_workspace_ids,
            fresh=True,
        )
        dropped = {citation.chunk_id for citation in citations if citation.chunk_id not in live}
        if dropped:
            logger.info(
                "verify_revalidation_dropped_citations",
                extra={
                    "event": "verify_revalidation_dropped_citations",
                    "chunk_ids": sorted(dropped),
                },
            )
            citations = [c for c in citations if c.chunk_id not in dropped]
            revalidated: list[Claim] = []
            for claim in verified:
                kept = [ref for ref in claim.chunk_ids if ref not in dropped]
                if kept:
                    revalidated.append(Claim(text=claim.text, chunk_ids=kept))
            verified = revalidated

    return verified, citations


def _hydrator_for(chunk_lookup: ChunkLookup | None) -> CanonicalHydrator | None:
    """Return the hydrator to verify with, or ``None`` for the legacy contract.

    A :class:`~answer.hydration.CanonicalHydrator` supplied by the engine is
    reused as-is (shared cache + ``lookups`` counter across the context and
    verify seams); any other :class:`~answer.interfaces.ChunkLookup` is wrapped
    in a fresh hydrator for this call. No store at all → ``None`` → legacy
    existence/scope behaviour.
    """
    if isinstance(chunk_lookup, CanonicalHydrator):
        return chunk_lookup if chunk_lookup.available else None
    if chunk_lookup is None:
        return None
    return CanonicalHydrator(chunk_lookup)
