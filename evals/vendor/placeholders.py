"""PLACEHOLDERS for official metrics whose scripts have not landed yet.

T-EVAL-007 discipline: **placeholders carry the exact output contract, never
a stand-in number.** A placeholder raises :class:`OfficialMetricNotVendored`
when called, naming the corpus ticket that must vendor the real script. The
harness's leaderboard tables show a placeholder column as `pending` — never
0.0, never a locally-computed value — so a reader cannot mistake a not-yet-run
official metric for a measured one.

Contract specs below are the exact shapes the official scripts publish, so the
landing ticke has a byte-level target.
"""

from __future__ import annotations

from evals.vendor import OfficialMetricNotVendored

_BFCL_SPEC = """
BFCL AST-accuracy contract (upstream: gorilla/berkeley-function-call-leaderboard):

  bfcl_ast_accuracy(per_item: list[AstComparison]) -> dict[str, float]

  AstComparison = namedtuple("AstComparison", ["pred_calls", "gold_calls"]) where
  each call is {"name": str, "arguments": {...}} with types exactly as the
  upstream AST comparison treats them (numbers/strings/bools/lists/dicts
  compared recursively; the upstream applies its documented type-coercion
  rules for string-vs-number argument values).

  Returns {"total": int, "correct": int, "ast_accuracy": float} where an item
  counts as correct when EVERY gold call is matched by an AST-equal predicted
  call (order-insensitive on the call set, per the upstream), and
  ast_accuracy == correct / total (0.0 when total == 0).

  The landed script must be vendored verbatim into evals/vendor/bfcl_eval.py
  and registered in evals/vendor/__init__.py before this placeholder is
  removed.
"""

_TWOWIKI_SPEC = """
2WikiMultihopQA EM/F1 contract (upstream: ans-4175/2WikiMultihopQA):

  twowiki_metrics(per_item: list[AnswerPair]) -> dict[str, float]

  AnswerPair = namedtuple("AnswerPair", ["gold", "pred", "supporting_facts"])
  with the HotpotQA shape; the upstream script publishes answer EM/F1 plus
  supporting-fact metrics with the HotpotQA definitions, so the vendored
  implementation must match hotpot_evaluate_v1.py's normalizer byte-for-byte
  (it does in the upstream repository).
"""

_MUSIQUE_SPEC = """
MuSiQue EM/F1 contract (upstream: stonybrooknlp/musique):

  musique_metrics(per_item: list[AnswerPair]) -> dict[str, float]

  Same answer EM/F1 contract as 2Wiki (HotpotQA-style normalization); the
  upstream publishes the same metric names.
"""


def bfcl_ast_accuracy(*args: object, **kwargs: object) -> dict[str, float]:
    """Raised until the official BFCL script is vendored."""
    raise OfficialMetricNotVendored(
        "bfcl_ast_accuracy: the official script is not vendored yet "
        "(T-EVAL-00x). Contract spec in evals/vendor/placeholders.py docstring."
        + _BFCL_SPEC
    )


def twowiki_answer_em(*args: object, **kwargs: object) -> float:
    raise OfficialMetricNotVendored(
        "twowiki_answer_em: the official script is not vendored yet "
        "(T-EVAL-00x). Contract:" + _TWOWIKI_SPEC
    )


def twowiki_answer_f1(*args: object, **kwargs: object) -> float:
    raise OfficialMetricNotVendored(
        "twowiki_answer_f1: the official script is not vendored yet "
        "(T-EVAL-00x). Contract:" + _TWOWIKI_SPEC
    )


def musique_answer_em(*args: object, **kwargs: object) -> float:
    raise OfficialMetricNotVendored(
        "musique_answer_em: the official script is not vendored yet "
        "(T-EVAL-00x). Contract:" + _MUSIQUE_SPEC
    )


def musique_answer_f1(*args: object, **kwargs: object) -> float:
    raise OfficialMetricNotVendored(
        "musique_answer_f1: the official script is not vendored yet "
        "(T-EVAL-00x). Contract:" + _MUSIQUE_SPEC
    )
