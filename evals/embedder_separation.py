"""Embedder separation protocol for T-EVAL-008.

Proves that the EVAL embedder (``google/gemini-embedding-2``, 3072-d, cloud)
separates related from unrelated text in ENGLISH and ROMANIAN, with the
production embedder (``BAAI/bge-m3``, 1024-d, local) run through the IDENTICAL
protocol as the comparison leg.

NO-CONFOUNDING IS STRUCTURAL, NOT PROMISED. Every number in this module is
produced by the same three functions regardless of which embedder is passed in:

  * :func:`embed_all`      — the only place texts are embedded, one batching
                             rule for both legs;
  * :func:`cosine`         — the only similarity function, and it normalises BOTH
                             vectors itself, so no embedder's own normalisation
                             convention can leak into the comparison;
  * :func:`evidence_recall_at_k` — the only retrieval metric, called with the
                             same ``k`` values for both legs.

The embedder is the only argument that changes. Texts, k, and metric code are
shared constants.

Nothing here scores a model answer or touches a live collection: the cloud leg
sees eval and synthetic text only, and bge-m3 remains the production embedder.
"""

from __future__ import annotations

import dataclasses
import json
import math
import statistics
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from evals import hotpot_full
from evals.embedder_clients import EmbedderError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evals.embedder_clients import Embedder

#: Batching size for BOTH legs. One number, so neither leg can be advantaged by
#: a different request shape.
#:
#: Reduced from 32 to 8 for T-EVAL-008b. At 32 the HotpotQA probe sent 32
#: whole Wikipedia paragraphs in one request and the host OOM killer took out
#: the production infinity service: dmesg recorded the victim at anon-rss
#: 5,133,472kB / total-vm 24,807,344kB, roughly double the ~2.8GB the models
#: need at rest. This is a HOST-RESIDENT-MEMORY limit, not a batch-count limit,
#: so the only lever that matters is how many long texts are in flight at once.
#: It stays a single shared constant: lowering it for both legs preserves the
#: no-confounding guarantee (identical texts, identical k, identical batching).
BATCH_SIZE = 8

#: museR's sanity bound from the card, quoted only as a bound to sanity-check
#: against. Every number reported here is measured fresh in this run.
#: His figures were for bge-m3: ~0.80 related vs ~0.44 unrelated.
COORDINATOR_BOUND_BGE_M3 = {"related": 0.80, "unrelated": 0.44}

#: Fixed shift used to build unrelated pairs. Chosen by checking that NO paired
#: concept shares a content word with its partner in any of the four word-set
#: directions (EN/EN, RO/RO, EN/RO, RO/EN). Shifts 4, 5, 9, 10, 11, 15 and 16
#: all satisfy that; 9 is used. An earlier shift of 7 did NOT: it paired the
#: vaccine concept with the caffeine one, which share "system"/"sistemul", and
#: would have quietly contaminated the unrelated set.
UNRELATED_SHIFT = 9


@dataclasses.dataclass(frozen=True, slots=True)
class Concept:
    """One topic, written once in English and faithfully in Romanian.

    A paraphrase field adds a same-language restatement, and a ``ro_question``
    adds a natural Romanian query over the same fact. Both are used to build
    probes; neither is shared between the two legs.
    """

    concept_id: int
    en: str
    ro: str
    en_paraphrase: str | None = None
    ro_question: str | None = None


#: Twenty deliberately distinct subjects, so any cross-pairing between them is
#: unrelated rather than merely different wording. Romanian is a faithful
#: translation of the English in every row (no diacritics, to stay ASCII and
#: avoid an encoding variable in the comparison).
CONCEPTS: tuple[Concept, ...] = (
    Concept(0, "Romania is a country in Eastern Europe.",
            "Romania este o tara din Europa de Est.",
            "Romania is a nation situated in Eastern Europe.",
            "In ce tara este situata Romania?"),
    Concept(1, "The Danube is the second longest river in Europe.",
            "Dunarea este al doilea cel mai lung rau din Europa.",
            "The Danube is the second-longest river on the continent.",
            "Care este al doilea cel mai lung rau din Europa?"),
    Concept(2, "A compiler translates source code into machine instructions.",
            "Un compilator translateaza codul sursa in instructiuni masina.",
            "A compiler turns source code into machine instructions.",
            "Ce instrument transforma codul sursa in instructiuni masina?"),
    Concept(3, "Photosynthesis converts light energy into chemical energy.",
            "Fotosinteza transforma energia luminoasa in energie chimica.",
            "Light energy is converted into chemical energy by photosynthesis.",
            "Ce proces transforma energia luminoasa in energie chimica?"),
    Concept(4, "The Transfagarasan highway crosses the Carpathian Mountains.",
            "Autostrada Transfagarasan traverseaza Carpatii.",
            "The Transfagarasan road passes through the Carpathian range.",
            "Ce autostrada traverseaza Carpatii?"),
    Concept(5, "Beethoven composed nine symphonies.",
            "Beethoven a compus noua simfonii.",
            "Beethoven wrote a total of nine symphonies.",
            "Cate simfonii a compus Beethoven?"),
    Concept(6, "Inflation is a sustained rise in the general price level.",
            "Inflatia este o crestere sustinuta a nivelului general al preturilor.",
            "A sustained broad increase in prices is called inflation.",
            "Ce inseamna cresterea sustinuta a nivelului general al preturilor?"),
    Concept(7, "Mount Everest is the highest mountain above sea level.",
            "Everestul este cel mai inalt munte deasupra nivelului marii.",
            "No mountain stands higher than Everest above sea level.",
            "Care este cel mai inalt munte deasupra nivelului marii?"),
    Concept(8, "Myocardial infarction is caused by a blockage of blood flow to the heart.",
            "Infarctul miocardic este cauzat de blocarea fluxului de sange spre inima.",
            "A heart attack happens when blood flow to the heart is blocked.",
            "Ce blocheaza fluxul de sange spre inima?"),
    Concept(9, "The Antarctic Treaty was signed in 1959.",
            "Tratatul Antarctic a fost semnat in 1959.",
            "In 1959 the Antarctic Treaty was signed.",
            "In ce an a fost semnat Tratatul Antarctic?"),
    Concept(10, "A vaccine trains the immune system to recognise a pathogen.",
            "Un vaccin antreneaza sistemul imunitar sa recunoasca un patogen.",
            "A vaccine teaches the immune system to recognise a pathogen."),
    Concept(11, "Basketball is played by five players per team on court.",
            "Baschetul se joaca cu cinci jucatori pe echipe in teren.",
            "Each basketball team fields five players on the court."),
    Concept(12, "The Black Sea lies between Romania and Turkey.",
            "Marea Neagra se afla intre Romania si Turcia.",
            "Romania and Turkey border the Black Sea."),
    Concept(13, "Entropy in thermodynamics measures disorder.",
            "Entropia in termodinamica masoara dezordinea.",
            "Disorder is what entropy measures in thermodynamics."),
    Concept(14, "Bucharest is the capital and largest city of Romania.",
            "Bucuresti este capitala si cel mai mare oras din Romania.",
            "Romania's capital is also its largest city, Bucharest."),
    Concept(15, "A prime number is divisible only by one and itself.",
            "Un numar prim este divizibil doar cu unu si cu el insusi.",
            "Only one and itself divide a prime number."),
    Concept(16, "The Voyager probe left the solar system in 2012.",
            "Sonda Voyager a parasit sistemul solar in 2012.",
            "In 2012 the Voyager probe departed the solar system."),
    Concept(17, "Coffee contains caffeine which stimulates the nervous system.",
            "Cafeaua contine cafeina care stimuleaza sistemul nervos.",
            "Caffeine in coffee stimulates the nervous system."),
    Concept(18, "The Dacian fortress of Sarmizegetusa is a UNESCO site.",
            "Cetatea dacica de la Sarmizegetusa este un sit UNESCO.",
            "Sarmizegetusa, a Dacian fortress, is a UNESCO site."),
    Concept(19, "A compiler's output can be optimised by a linker.",
            "Rezultatul unui compilator poate fi optimizat de un linker.",
            "A linker can optimise the output of a compiler."),
)


# --------------------------------------------------------------------------
# shared primitives — identical for both legs, by construction
# --------------------------------------------------------------------------


def embed_all(embedder: Embedder, texts: Sequence[str]) -> list[list[float]]:
    """Embed every text with the shared batching rule. The only embed call site."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        vectors.extend(embedder.embed(list(texts[start : start + BATCH_SIZE])))
    if len(vectors) != len(texts):
        raise ValueError(
            f"{embedder.name}: embedded {len(vectors)} vectors for {len(texts)} texts"
        )
    return vectors


def _unit(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        raise ValueError("zero vector: refusing to manufacture a similarity")
    return [x / norm for x in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, normalising BOTH sides here.

    Doing the normalisation inside this one function is deliberate: it makes the
    measurement independent of whatever normalisation each provider applied to
    its own vectors, so the two legs are compared on equal terms.
    """
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    return sum(x * y for x, y in zip(_unit(a), _unit(b), strict=True))


@dataclasses.dataclass(frozen=True, slots=True)
class PairSet:
    """A named collection of (left, right) text pairs."""

    name: str
    pairs: tuple[tuple[str, str], ...]

    def __len__(self) -> int:
        return len(self.pairs)


def translation_related() -> PairSet:
    """Same meaning, different language: EN_i with its faithful RO translation.

    Reported as two directional sets so a language-ordering effect cannot hide in
    an average.
    """
    forward = tuple((c.en, c.ro) for c in CONCEPTS)
    backward = tuple((c.ro, c.en) for c in CONCEPTS)
    return PairSet("related_en_ro", forward + backward)


def translation_unrelated() -> PairSet:
    """Different meaning, different language: EN_i against an unrelated RO_j.

    Built with the same texts and the same directional split as the related set,
    so the ONLY difference between the two sets is which concept is on the right.
    That is what makes the gap interpretable.
    """
    n = len(CONCEPTS)
    forward = tuple(
        (c.en, CONCEPTS[(c.concept_id + UNRELATED_SHIFT) % n].ro) for c in CONCEPTS
    )
    backward = tuple(
        (c.ro, CONCEPTS[(c.concept_id + UNRELATED_SHIFT) % n].en) for c in CONCEPTS
    )
    return PairSet("unrelated_en_ro", forward + backward)


def monolingual_related() -> PairSet:
    """Control: same meaning, SAME language. Catches a model that only does
    well across languages, and gives the cross-lingual numbers a baseline."""
    return PairSet("related_monolingual_en", tuple(_monolingual_rows()))


def _monolingual_rows() -> list[tuple[str, str]]:
    """(original, paraphrase) pairs, pre-narrowed to plain strings."""
    return [
        (c.en, c.en_paraphrase) for c in CONCEPTS if c.en_paraphrase is not None
    ]


def monolingual_unrelated() -> PairSet:
    """Control: different meaning, same language."""
    rows = _monolingual_rows()
    n = len(rows)
    return PairSet(
        "unrelated_monolingual_en",
        tuple(
            (left, rows[(index + UNRELATED_SHIFT) % n][1])
            for index, (left, _) in enumerate(rows)
        ),
    )


def all_similarity_texts() -> list[str]:
    """Every text any similarity set needs, de-duplicated, order-stable.

    Both legs embed exactly this list, so the cloud leg pays for each distinct
    text once and the two legs provably saw identical text.
    """
    seen: dict[str, None] = {}
    for pair_set in (
        translation_related(),
        translation_unrelated(),
        monolingual_related(),
        monolingual_unrelated(),
    ):
        for left, right in pair_set.pairs:
            seen.setdefault(left, None)
            seen.setdefault(right, None)
    return list(seen)


@dataclasses.dataclass(frozen=True, slots=True)
class SeparationStats:
    """Summary of one pair set under one embedder."""

    pair_set: str
    embedder: str
    n_pairs: int
    mean: float
    stdev: float
    minimum: float
    maximum: float

    def as_row(self) -> dict[str, object]:
        return {
            "pair_set": self.pair_set,
            "embedder": self.embedder,
            "n_pairs": self.n_pairs,
            "mean_cosine": round(self.mean, 4),
            "stdev": round(self.stdev, 4),
            "min": round(self.minimum, 4),
            "max": round(self.maximum, 4),
        }


def measure_pair_set(
    embedder: Embedder,
    pair_set: PairSet,
    vectors: dict[str, list[float]],
) -> SeparationStats:
    """Score one pair set using the shared cosine. Same code for both legs."""
    scores = [cosine(vectors[left], vectors[right]) for left, right in pair_set.pairs]
    if not scores:
        raise ValueError(f"pair set {pair_set.name} is empty")
    return SeparationStats(
        pair_set=pair_set.name,
        embedder=embedder.name,
        n_pairs=len(scores),
        mean=statistics.fmean(scores),
        stdev=statistics.stdev(scores) if len(scores) > 1 else 0.0,
        minimum=min(scores),
        maximum=max(scores),
    )


# --------------------------------------------------------------------------
# retrieval probe — same metric code, same k, both legs
# --------------------------------------------------------------------------


def evidence_recall_at_k(
    query_vector: Sequence[float],
    candidate_vectors: Sequence[Sequence[float]],
    gold_indices: Sequence[int],
    k: int,
) -> float:
    """Fraction of gold supporting items retrieved in the top k.

    Shared by both legs and called with identical k. Ranking is by the shared
    :func:`cosine`; ties break by candidate index, so the ordering is
    deterministic and identical in structure for both embedders.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    if not gold_indices:
        raise ValueError("gold_indices must not be empty")
    scored = [cosine(query_vector, vec) for vec in candidate_vectors]
    order = sorted(range(len(scored)), key=lambda i: (-scored[i], i))
    top = set(order[:k])
    return sum(1 for index in gold_indices if index in top) / len(gold_indices)


@dataclasses.dataclass(frozen=True, slots=True)
class RetrievalCase:
    """One retrieval probe: a query, its candidate pool, and the gold indices."""

    case_id: str
    query: str
    candidates: tuple[str, ...]
    gold_indices: tuple[int, ...]


def hotpot_cases(n_records: int = 40) -> list[RetrievalCase]:
    """Gold HotpotQA probe cases from the corpus acquired in T-EVAL-001ACQUIRE.

    Uses ``distractor/validation`` — the split where every gold supporting title
    actually resolves to a paragraph in the candidate set. Scoring retrieval on
    ``fullwiki/validation`` would measure a data artifact (5,316 of 7,405 records
    have no resolvable gold span), not retrieval quality.

    Records whose gold titles do not resolve are skipped rather than counted as
    failures, and the skip count is reported.
    """
    cases: list[RetrievalCase] = []
    skipped = 0
    for record in hotpot_full.iter_records("validation", config="distractor"):
        if not record.is_labeled or not record.all_gold_titles_present:
            skipped += 1
            continue
        titles = [p.title for p in record.paragraphs]
        gold = tuple(
            sorted({titles.index(fact.title) for fact in record.supporting_facts})
        )
        cases.append(
            RetrievalCase(
                case_id=record.record_id,
                query=record.question,
                candidates=tuple(p.text for p in record.paragraphs),
                gold_indices=gold,
            )
        )
        if len(cases) >= n_records:
            break
    hotpot_case_skips["skipped_unresolvable"] = skipped
    return cases


#: Populated by :func:`hotpot_cases` so the report can state the skip count
#: instead of leaving a silent gap in the case list.
hotpot_case_skips: dict[str, int] = {"skipped_unresolvable": 0}

SYNTHETIC_K_VALUES = (1, 3, 5)
#: Headline k for the probe, identical for both legs.
HEADLINE_K = 3


def synthetic_ro_cases() -> list[RetrievalCase]:
    """Romanian retrieval probe: a 20-paragraph pool, 10 Romanian queries.

    Synthetic and deliberately small — it is a sanity probe on Romanian query
    behaviour, NOT a benchmark, and is reported separately from the HotpotQA
    number so the two are never added together.
    """
    pool = tuple(c.ro for c in CONCEPTS)
    cases: list[RetrievalCase] = []
    for concept in CONCEPTS:
        if concept.ro_question is None:
            continue
        cases.append(
            RetrievalCase(
                case_id=f"ro-{concept.concept_id}",
                query=concept.ro_question,
                candidates=pool,
                gold_indices=(concept.concept_id,),
            )
        )
    return cases


@dataclasses.dataclass(frozen=True, slots=True)
class RetrievalStats:
    """Retrieval probe summary for one embedder at one k."""

    probe: str
    embedder: str
    k: int
    n_cases: int
    evidence_recall: float

    def as_row(self) -> dict[str, object]:
        return {
            "probe": self.probe,
            "embedder": self.embedder,
            "k": self.k,
            "n_cases": self.n_cases,
            "evidence_recall": round(self.evidence_recall, 4),
        }


def run_retrieval_probe(
    embedder: Embedder,
    probe_name: str,
    cases: Sequence[RetrievalCase],
    k_values: Iterable[int] = SYNTHETIC_K_VALUES,
) -> list[RetrievalStats]:
    """Run the probe for every k, embedding each query and candidate once."""
    query_vectors = embed_all(embedder, [c.query for c in cases])
    flat: list[str] = []
    for case in cases:
        flat.extend(case.candidates)
    candidate_vectors = embed_all(embedder, flat)

    # Slice the flat candidate vector list ONCE. Advancing the offset inside the
    # k loop (an earlier bug here) walked past the end on the second k and
    # reported EvidenceRecall 0.0 for every k > 1 — a monotonically decreasing
    # recall that is impossible by construction.
    slices: list[list[list[float]]] = []
    offset = 0
    for case in cases:
        width = len(case.candidates)
        slices.append(candidate_vectors[offset : offset + width])
        offset += width

    results: list[RetrievalStats] = []
    for k in k_values:
        recalls = [
            evidence_recall_at_k(query_vectors[index], slices[index],
                                 case.gold_indices, k)
            for index, case in enumerate(cases)
        ]
        results.append(
            RetrievalStats(
                probe=probe_name,
                embedder=embedder.name,
                k=k,
                n_cases=len(cases),
                evidence_recall=statistics.fmean(recalls),
            )
        )
    return results


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def separation_leg(embedder: Embedder) -> list[SeparationStats]:
    """Run every similarity pair set for one embedder. Identical for both legs."""
    texts = all_similarity_texts()
    vectors = dict(zip(texts, embed_all(embedder, texts), strict=True))
    return [
        measure_pair_set(embedder, pair_set, vectors)
        for pair_set in (
            translation_related(),
            translation_unrelated(),
            monolingual_related(),
            monolingual_unrelated(),
        )
    ]


def build_report(
    embedders: Sequence[Embedder],
    *,
    n_hotpot: int = 40,
    legs_not_run: dict[str, str] | None = None,
) -> dict[str, object]:
    """Run both legs end to end and return the full number record."""
    sep_all: list[SeparationStats] = []
    retrieval: list[dict[str, object]] = []
    not_run: dict[str, str] = dict(legs_not_run or {})
    for embedder in embedders:
        # A leg that dies mid-run is RECORDED and skipped, not allowed to
        # discard the other leg's metered work, and never silently dropped:
        # it lands in legs_not_run with the endpoint's own error, which is
        # printed, and it clears full_run.
        try:
            sep_all.extend(separation_leg(embedder))
            retrieval.extend(
                row.as_row()
                for row in run_retrieval_probe(
                    embedder, "hotpotqa_distractor_validation", hotpot_cases(n_hotpot)
                )
            )
            retrieval.extend(
                row.as_row()
                for row in run_retrieval_probe(
                    embedder, "synthetic_ro", synthetic_ro_cases()
                )
            )
        except EmbedderError as exc:
            not_run[embedder.name] = f"leg failed during the run: {exc}"

    margins: dict[str, dict[str, float]] = {}
    needed = {
        "related_en_ro",
        "unrelated_en_ro",
        "related_monolingual_en",
        "unrelated_monolingual_en",
    }
    for embedder in embedders:
        by_set = {
            stats.pair_set: stats.mean
            for stats in sep_all
            if stats.embedder == embedder.name
        }
        missing = needed - by_set.keys()
        if missing:
            # A leg that died before finishing its separation sets must not be
            # reported as a margin computed from whatever it managed to emit.
            not_run.setdefault(
                embedder.name,
                f"no margin computed: missing separation sets {sorted(missing)}",
            )
            continue
        margins[embedder.name] = {
            "related_crosslingual": round(
                by_set["related_en_ro"] - by_set["unrelated_en_ro"], 4
            ),
            "related_monolingual": round(
                by_set["related_monolingual_en"]
                - by_set["unrelated_monolingual_en"],
                4,
            ),
        }

    return {
        "embedders": [
            {"name": e.name, "dimension": e.dimension, "priced": e.priced}
            for e in embedders
        ],
        "separation": [stats.as_row() for stats in sep_all],
        "retrieval": retrieval,
        "margins": [
            {
                "embedder": name,
                "related_crosslingual": values["related_crosslingual"],
                "related_monolingual": values["related_monolingual"],
            }
            for name, values in margins.items()
        ],
        "cost": [embedder.stats.as_row() for embedder in embedders],
        "legs_not_run": [
            {"leg": name, "reason": reason} for name, reason in not_run.items()
        ],
        # False means DO NOT read this as a two-leg comparison.
        "full_run": not not_run and len(embedders) >= 2,
        "failure_rates": [
            {
                "endpoint": embedder.name,
                "calls": embedder.stats.n_calls,
                "failed_calls": embedder.stats.n_failed_calls,
                "retries": embedder.stats.n_retries,
                "failure_rate": round(embedder.stats.failure_rate, 4),
            }
            for embedder in embedders
        ],
        "hotpot_cases_skipped": hotpot_case_skips["skipped_unresolvable"],
        "coordinator_bound_bge_m3": COORDINATOR_BOUND_BGE_M3,
    }


def _rows(report: dict[str, object], key: str) -> list[dict[str, object]]:
    """Narrow a report section to rows, so the printer stays type-safe."""
    value = report[key]
    if not isinstance(value, list):
        raise TypeError(f"report[{key!r}] is {type(value).__name__}, expected list")
    return [dict(row) for row in value if isinstance(row, dict)]


def _section(report: dict[str, object], key: str) -> dict[str, object]:
    value = report[key]
    if not isinstance(value, dict):
        raise TypeError(f"report[{key!r}] is {type(value).__name__}, expected mapping")
    return dict(value)


def _table(rows: Sequence[dict[str, object]], columns: Sequence[str]) -> str:
    header = " | ".join(f"{c:>22}" for c in columns)
    rule = "-+-".join("-" * 22 for _ in columns)
    body = "\n".join(
        " | ".join(f"{row.get(c, '')!s:>22}" for c in columns) for row in rows
    )
    return f"{header}\n{rule}\n{body}"


def main(
    *,
    n_hotpot: int = 40,
    legs: Sequence[str] = ("gemini", "bge_m3"),
) -> dict[str, object]:
    """Run the requested legs, print the number tables, return the report.

    ``legs`` exists so a DOWN endpoint can be excluded explicitly rather than by
    editing the protocol. Any leg not run is recorded in the report under
    ``legs_not_run`` WITH ITS REASON, so a partial run is impossible to mistake
    for a full one. Omitting a leg is a decision, and the report carries it.
    """
    from evals.embedder_clients import BgeM3Local, GeminiEmbedding2

    factories: dict[str, type[Embedder]] = {
        "gemini": GeminiEmbedding2,
        "bge_m3": BgeM3Local,
    }
    unknown = [name for name in legs if name not in factories]
    if unknown:
        raise ValueError(f"unknown leg(s) {unknown}; known legs: {sorted(factories)}")

    embedders: list[Embedder] = []
    legs_not_run: dict[str, str] = {}
    for name in factories:
        if name in legs:
            embedders.append(factories[name]())
        else:
            legs_not_run[name] = "not requested for this run"
    try:
        report = build_report(embedders, n_hotpot=n_hotpot, legs_not_run=legs_not_run)
    finally:
        for embedder in embedders:
            embedder.close()

    print("SEPARATION (mean cosine; identical texts, identical metric code)")
    print(
        _table(
            _rows(report, "separation"),
            ["embedder", "pair_set", "n_pairs", "mean_cosine", "stdev", "min", "max"],
        )
    )
    print("\nMARGINS (related minus unrelated; positive = separates)")
    print(
        _table(
            _rows(report, "margins"),
            ["embedder", "related_crosslingual", "related_monolingual"],
        )
    )
    print("\nRETRIEVAL PROBE (EvidenceRecall; identical metric code and k)")
    print(
        _table(
            _rows(report, "retrieval"),
            ["probe", "embedder", "k", "n_cases", "evidence_recall"],
        )
    )
    print("\nCOST")
    print(
        _table(
            _rows(report, "cost"),
            ["endpoint", "calls", "vectors", "prompt_tokens", "cost_usd",
             "cost_is_metered"],
        )
    )
    print("\nFAILURE RATES")
    print(
        _table(
            _rows(report, "failure_rates"),
            ["endpoint", "calls", "failed_calls", "retries", "failure_rate"],
        )
    )
    print("\nLEGS NOT RUN")
    not_run_rows = _rows(report, "legs_not_run")
    if not_run_rows:
        print(_table(not_run_rows, ["leg", "reason"]))
        print(
            "  !! PARTIAL RUN - this is NOT a two-leg comparison. Any conclusion "
            "about gemini vs bge-m3 is unsupported until every leg ran."
        )
    else:
        print("  (none - both legs completed)")
    if not report.get("full_run", False):
        print("  full_run = False")
    return report


if __name__ == "__main__":  # pragma: no cover - CLI entry
    print(json.dumps(main(), indent=2, sort_keys=True, default=str))