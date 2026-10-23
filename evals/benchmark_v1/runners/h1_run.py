"""H1 closed-context reader A/B — execution (plan §3.2 / §4.2 / §12 Phase 2).

Architecture, per the preregistered frozen protocol:

    (question_id, repeat) -> ONE shared first draft (metered once)
                             -> OFF arm: ExtractiveGrounder   -> repair/fallback
                             -> ON  arm: ParaphraseTolerantGrounder -> repair/fallback

The first draft is generated ONCE and hashed; both arms consume the identical
draft string, so the A/B isolates the grounder's decision logic and the
generation cost is paid once per (question, repeat), not twice.

Deterministic scoring only (answer EM/F1, abstention, citation validity). Strict
GSR cells are deliberately NOT produced here — the rubric is still PROPOSAL.

Safety: the worst-case arithmetic is computed and gated BEFORE the first metered
call; every metered call is appended to the shared budget ledger under a run tag.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import hashlib
import json
import pathlib
import sys
import time

_REPO = pathlib.Path(__file__).resolve().parents[3]
for _p in (_REPO / "packages", _REPO / "apps" / "api", _REPO / "apps" / "worker",
           _REPO / "evals" / "vendor", str(_REPO / "evals" / "benchmark_v1" / "runners")):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.verifier import ParaphraseTolerantGrounder  # noqa: E402
from answer.verification import ExtractiveGrounder  # noqa: E402
from models.adapters.openrouter_provider import (  # noqa: E402
    DEFAULT_CHAT_FREE_MODEL,
    DEFAULT_CHAT_PAID_MODEL,
    GLOBAL_ACCOUNTING,
    OpenRouterProvider,
)
from models.interfaces import ChatMessage, GenerationRequest, MessageRole  # noqa: E402

RUNS = _REPO / "evals" / "benchmark_v1" / "runs" / "h1"
LEDGER = _REPO / "evals" / "benchmark_v1" / "reports" / "budget_ledger.csv"
ARITH = _REPO / "evals" / "benchmark_v1" / "reports" / "h1_pre_call_arithmetic.json"
RUN_TAG = "h1_reader_ab"

#: Preregistered total cap for the whole benchmark.
TOTAL_CAP_USD = 2.00
#: Fallback remaining cap if the ledger cannot be read (H1 entry value).
REMAINING_CAP_USD = 1.999109
#: P02-measured worst case for ONE (1 embed + 1 generate) unit.
WORST_UNIT_USD = 0.00011097
#: Max repair attempts per arm (repair is the only extra metered leg).
MAX_REPAIRS_PER_ARM = 1

#: The H1 BASELINE reader prompt, preserved verbatim so the committed H1 run stays
#: reproducible byte-for-byte. Do not edit this string: it IS the T-RP01 control.
DRAFT_PROMPT = (
    "Answer the question using ONLY the numbered context paragraphs. "
    "Cite the paragraph number for every claim. If the context does not support "
    "an answer, reply exactly with ABSTAIN.\n\n"
    "{ctx}\n\nQuestion: {q}\n\n"
    'Reply with JSON only: {{"answer": "...", "abstained": false, '
    '"claims": [{{"text": "...", "paragraph": 1}}]}}'
)

#: T-RP01 REVISED reader prompt -- the minimal change that narrows refusal to
#: genuinely-missing-evidence cases.
#:
#: Diagnosis of the 18.4% baseline abstention (all at the shared-draft level, identical
#: in both arms, so it is a READER issue and not a grounder issue):
#:
#:   1. "If the context does not support AN ANSWER, reply exactly with ABSTAIN."
#:      All-or-nothing bar. HotpotQA/2wiki are multi-hop, so partial evidence -- one hop
#:      found, second hop missing -- fails "supports an answer". Measured: 60 of 64
#:      baseline refusals occurred on items whose gold answer string WAS present in the
#:      context, i.e. 93.75% were false refusals under rubric §6.
#:   2. "reply exactly with ABSTAIN" is a zero-cost escape hatch with no stated downside
#:      and no instruction to prefer attempting.
#:   3. The JSON contract offers only a boolean `abstained` flag -- no way to express
#:      "here is the part I can support, and this is what is missing", so partial
#:      competence had no representation and full refusal was the cheaper reply.
#:
#: The revision keeps the escape hatch for the genuine case (no paragraph supports ANY
#: part of the answer) and makes partial-but-cited answering the instructed default.
DRAFT_PROMPT_REVISED = (
    "Answer the question using ONLY the numbered context paragraphs. "
    "Cite the paragraph number for every claim.\n"
    "Answer whenever at least one paragraph supports any part of the answer. These "
    "questions need several facts, so finding one hop and not the other is normal: give "
    "the partial answer you can support from the paragraphs and cite them. A missing "
    "hop is not a reason to refuse.\n"
    "Reply exactly with ABSTAIN only when NO paragraph in the context supports any part "
    "of the answer.\n\n"
    "{ctx}\n\nQuestion: {q}\n\n"
    'Reply with JSON only: {{"answer": "...", "abstained": false, '
    '"claims": [{{"text": "...", "paragraph": 1}}]}}'
)

#: T-RP01b REVISED+CAP variant -- RP01's narrowed refusal rule with the elaboration
#: clause REMOVED and an explicit answer-length cap ADDED.
#:
#: T-RP01 measured the narrowed refusal rule and found it works (refusal 17.33% ->
#: 2.67%) but that it also inflated answers on items the reader was ALREADY answering
#: (ON arm 20.6 -> 22.9 mean words, F1 0.2548 -> 0.1891). The decomposition showed the
#: regression came entirely from that verbosity, not from the rescued items (which all
#: gained). So this variant keeps the refusal half and removes the verbosity half.
#:
#: Two deliberate choices:
#:   * The cap applies to the ANSWER FIELD ONLY, not to the claims. Claims carry the
#:     citations the grounders adjudicate; capping them would change the grounding
#:     contract and confound the comparison. The regression was in answer verbosity
#:     diluting token-F1 against short gold spans, and that is what is capped here.
#:   * The cap is 20 words because the BASELINE mean on already-answered items was
#:     20.6 words. The ticket's success criterion is "length means back in band", so the
#:     cap is set to the baseline band rather than to whatever maximises F1.
DRAFT_PROMPT_CAPPED = (
    "Answer the question using ONLY the numbered context paragraphs. "
    "Cite the paragraph number for every claim.\n"
    "Answer whenever at least one paragraph supports any part of the answer. These "
    "questions need several facts, so finding one hop and not the other is normal. "
    "A missing hop is not a reason to refuse.\n"
    "Keep the answer field to 20 words or fewer: give the shortest answer the cited "
    "paragraphs support, with no preamble, no restatement of the question and no extra "
    "background.\n"
    "Reply exactly with ABSTAIN only when NO paragraph in the context supports any part "
    "of the answer.\n\n"
    "{ctx}\n\nQuestion: {q}\n\n"
    'Reply with JSON only: {{"answer": "...", "abstained": false, '
    '"claims": [{{"text": "...", "paragraph": 1}}]}}'
)

#: Reader-prompt variants selectable by the runner.
PROMPTS = {
    "baseline": DRAFT_PROMPT,
    "revised": DRAFT_PROMPT_REVISED,
    "capped": DRAFT_PROMPT_CAPPED,
}

REPAIR_PROMPT = (
    "The following claims were NOT supported verbatim by the cited paragraph. "
    "Rewrite them so each is directly supported by that paragraph's text, or drop "
    "them. Do not add new facts.\n\n"
    "{ctx}\n\nUnsupported claims:\n{claims}\n\n"
    'Reply with JSON only: {{"claims": [{{"text": "...", "paragraph": 1}}]}}'
)


class BudgetGate(RuntimeError):
    """Worst case breaches the remaining cap — spend nothing."""


def spent_to_date() -> float:
    """Actual metered spend recorded in the ledger (projections excluded)."""
    if not LEDGER.exists():
        return 0.0
    total = 0.0
    with LEDGER.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("row_type") != "metered_call":
                continue
            try:
                total += float(row.get("cost_usd") or 0)
            except ValueError:
                continue
    return total


def remaining_cap() -> float:
    return round(TOTAL_CAP_USD - spent_to_date(), 6)


def pre_call_arithmetic(n_attempts: int) -> dict[str, object]:
    """Worst-case cost model, computed BEFORE the first metered call."""
    units_per_attempt = 1 + 2 * MAX_REPAIRS_PER_ARM  # shared draft + 2 arms' repairs
    worst_per_attempt = units_per_attempt * WORST_UNIT_USD
    worst_total = worst_per_attempt * n_attempts
    out = {
        "run_tag": RUN_TAG,
        "computed_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "n_attempts": n_attempts,
        "shared_drafts": n_attempts,
        "arms": 2,
        "max_repairs_per_arm": MAX_REPAIRS_PER_ARM,
        "metered_units_per_attempt": units_per_attempt,
        "worst_unit_usd": WORST_UNIT_USD,
        "worst_per_attempt_usd": round(worst_per_attempt, 8),
        "worst_total_usd": round(worst_total, 6),
        "total_cap_usd": TOTAL_CAP_USD,
        "spent_to_date_usd": round(spent_to_date(), 8),
        "remaining_cap_usd": remaining_cap(),
        "note": (
            "Generation is free-first with paid fallback, so the worst case "
            "charges the PAID rate for every unit. P02 measured 16/16 fallbacks, "
            "so the free tier is not assumed to save anything."
        ),
    }
    if worst_total >= remaining_cap():
        out["verdict"] = "BUDGET-GATE"
        raise BudgetGate(
            f"worst case ${worst_total:.4f} >= remaining ${remaining_cap():.4f}; "
            "not starting"
        )
    out["verdict"] = "PROCEED"
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot() -> dict[str, float]:
    t = GLOBAL_ACCOUNTING.summary()["total"]
    return {k: float(t.get(k, 0) or 0) for k in
            ("calls", "free_calls", "paid_calls", "fallbacks", "failed_calls",
             "prompt_tokens", "completion_tokens", "cost_usd")}


def _delta(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    return {k: round(b[k] - a[k], 10) for k in a}


def _parse_json(text: str) -> dict:
    """Tolerant parse: models sometimes wrap JSON in prose or fences."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t.strip("`")
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        return json.loads(t[start : end + 1])
    except json.JSONDecodeError:
        return {}


class Harness:
    def __init__(self, run_tag: str = RUN_TAG) -> None:
        self.run_tag = run_tag
        self.provider = OpenRouterProvider(default_call_site=run_tag)
        self.grounder_off = ExtractiveGrounder()
        self.grounder_on = ParaphraseTolerantGrounder(threshold=0.85)
        self.ledger_rows: list[dict] = []

    def context_block(self, paragraphs: list[dict]) -> str:
        return "\n\n".join(
            f"[{i}] {p['title']}\n{p['text']}" for i, p in enumerate(paragraphs, 1)
        )

    async def generate(self, prompt: str, site: str) -> tuple[dict, str, float]:
        before = _snapshot()
        t0 = time.monotonic()
        err = ""
        payload: dict = {}
        model_used = ""
        try:
            res = await self.provider.generate(
                GenerationRequest(
                    model=DEFAULT_CHAT_FREE_MODEL,
                    messages=[ChatMessage(role=MessageRole.USER, content=prompt)],
                ),
                call_site=site,
            )
            payload = _parse_json(res.text or "")
            model_used = res.model or ""
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:200]
        dt = int((time.monotonic() - t0) * 1000)
        d = _delta(before, _snapshot())
        self.ledger_rows.append({
            "timestamp_utc": datetime.datetime.now(datetime.UTC).isoformat(),
            "run_tag": self.run_tag,
            # Real metered spend, not a projection. Required so spend_to_date()
            # counts this row and projection rows stay excluded.
            "row_type": "metered_call",
            "call_site": site,
            "model_requested": DEFAULT_CHAT_FREE_MODEL,
            "model_used": model_used or (DEFAULT_CHAT_PAID_MODEL if d["fallbacks"] else ""),
            "calls": int(d["calls"]),
            "free_calls": int(d["free_calls"]),
            "paid_calls": int(d["paid_calls"]),
            "fallbacks": int(d["fallbacks"]),
            "failed_calls": int(d["failed_calls"]),
            "prompt_tokens": int(d["prompt_tokens"]),
            "completion_tokens": int(d["completion_tokens"]),
            "cost_usd": d["cost_usd"],
            "latency_ms": dt,
            "error": err,
        })
        return payload, err, d["cost_usd"]

    def ground(self, claims: list[dict], paragraphs: list[dict], grounder) -> list[dict]:
        """Per-claim grounding against the CITED paragraph (deterministic)."""
        kept = []
        for c in claims:
            text = str(c.get("text", "")).strip()
            if not text:
                continue
            try:
                idx = int(c.get("paragraph", 0)) - 1
            except (TypeError, ValueError):
                idx = -1
            if not (0 <= idx < len(paragraphs)):
                kept.append({**c, "grounded": False, "grounding_score": 0.0,
                             "reason": "citation_out_of_range"})
                continue
            para = paragraphs[idx]["text"]
            score = float(grounder.score(text, para))
            kept.append({
                **c,
                "grounded": bool(score >= grounder.threshold),
                "grounding_score": round(score, 6),
                "grounded_against_title": paragraphs[idx]["title"],
                "threshold": grounder.threshold,
            })
        return kept

    async def repair(self, ctx: str, ungrounded: list[dict], site: str):
        """One bounded, metered repair of the ungrounded claims only."""
        listing = "\n".join(f"- {c['text']} (paragraph {c.get('paragraph')})"
                            for c in ungrounded)
        payload, err, cost = await self.generate(
            REPAIR_PROMPT.format(ctx=ctx, claims=listing), site)
        return payload.get("claims", []) or [], err, cost

async def run_attempt(h: Harness, item: dict, repeat: int,
                      prompt: str = DRAFT_PROMPT, run_tag: str = RUN_TAG) -> dict:
    """One (question, repeat): ONE shared draft, then both arms.

    The shared draft is generated once and both arms ground the SAME string; the
    draft hash is recorded so cross-arm identity is provable after the fact.
    """
    item_id = item["item_id"]
    ctx = h.context_block(item["paragraphs"])
    site = f"{run_tag}_{item_id}_r{repeat}"

    draft, draft_err, draft_cost = await h.generate(
        prompt.format(ctx=ctx, q=item["question"]), site + "_draft")
    draft_text = str(draft.get("answer", "") or "")
    draft_hash = _sha(json.dumps(draft, sort_keys=True))
    claims = [c for c in (draft.get("claims") or []) if isinstance(c, dict)]

    record: dict = {
        "run_tag": run_tag,
        "item_id": item_id,
        "dataset": item["dataset"],
        "repeat": repeat,
        "question": item["question"],
        "question_sha256": item["question_sha256"],
        "context_sha256": item["context_sha256"],
        "n_paragraphs": len(item["paragraphs"]),
        # T-P01 TRACE-GAP fix: the retrieved passage TEXT is persisted verbatim so
        # a future §4.1 replay can re-ground this exact draft offline.
        "paragraphs": item["paragraphs"],
        "generator_model_requested": DEFAULT_CHAT_FREE_MODEL,
        "generator_model_paid_fallback": DEFAULT_CHAT_PAID_MODEL,
        # Fork point (plan §4.2): T-G01 is post-generation, so the arms diverge
        # strictly AFTER this shared draft; nothing upstream differs.
        "fork_point": "post_generation",
        "shared_draft_hash": draft_hash,
        "shared_draft_answer": draft_text,
        "shared_draft_abstained": bool(draft.get("abstained", False)),
        "draft_error": draft_err,
        "draft_cost_usd": draft_cost,
        "arms": {},
    }

    for arm, grounder in (("off", h.grounder_off), ("on", h.grounder_on)):
        grounded = h.ground(claims, item["paragraphs"], grounder)
        ungrounded = [c for c in grounded if not c["grounded"]]
        repair_cost = 0.0
        repair_error = ""
        repaired: list[dict] = []
        if ungrounded and item["answerable"]:
            repaired, repair_error, repair_cost = await h.repair(
                ctx, ungrounded, site + f"_repair_{arm}")
        # Fallback: keep only claims the arm could ground (repair may still fail).
        accepted = [c for c in grounded if c["grounded"]]
        accepted += [c for c in h.ground(repaired, item["paragraphs"], grounder) if c["grounded"]]
        record["arms"][arm] = {
            "claims_total": len(claims),
            "claims_grounded": len(accepted),
            "final_answer": draft_text if accepted else "",
            "abstained": bool(draft.get("abstained")) and not accepted,
            "repair_attempted": bool(ungrounded and item["answerable"]),
            "repair_error": repair_error,
            "repair_cost_usd": repair_cost,
            "citations": [
                {"chunk_id": c["grounded_against_title"], "paragraph": c.get("paragraph"),
                 "grounding_score": c["grounding_score"], "text": c["text"]}
                for c in accepted
            ],
            "claims": accepted,
            "ungrounded_claims": ungrounded,
            "fallback_applied": bool(ungrounded and not accepted),
        }
    return record


#: The canonical ledger schema. `row_type` is what stops a projection row from being
#: read as metered spend. This MUST stay identical to FIELDS in
#: scripts/normalize_ledger.py -- appending with a different field list is what made
#: the ledger invalid CSV twice (T-E01, then again here), so the schema is now written
#: out in full on every append and never inferred from the existing header.
LEDGER_FIELDS = [
    "timestamp_utc", "run_tag", "row_type", "call_site",
    "model_requested", "model_used",
    "calls", "free_calls", "paid_calls", "fallbacks", "failed_calls",
    "prompt_tokens", "completion_tokens", "cost_usd", "latency_ms", "error",
    "detail",
]


def write_ledger(rows: list[dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    exists = LEDGER.exists()
    header = LEDGER.read_text(encoding="utf-8").splitlines()[0] if exists else ""
    canonical = ",".join(LEDGER_FIELDS)
    if exists and header != canonical:
        # Appending under a mismatched header is what corrupted this file before.
        # Refuse loudly rather than silently producing another unreadable ledger.
        raise RuntimeError(
            "budget_ledger.csv header does not match the canonical schema; run "
            "scripts/normalize_ledger.py first (do not append blind)"
        )
    with LEDGER.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LEDGER_FIELDS)
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in LEDGER_FIELDS})


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="0 = all runnable attempts")
    ap.add_argument("--dry-plan", action="store_true")
    ap.add_argument("--prompt", choices=sorted(PROMPTS), default="baseline",
                    help="reader prompt variant: baseline (H1 control) or revised (T-RP01)")
    ap.add_argument("--out-dir", default="", help="override the run output directory")
    ap.add_argument("--run-tag", default="", help="override the ledger/run tag")
    ap.add_argument("--datasets", default="hotpot",
                    help="comma-separated slices to run (default: hotpot only)")
    ap.add_argument("--repeats", type=int, default=0,
                    help="override R per item (0 = the slice's frozen R)")
    args = ap.parse_args()

    from h1_corpus import load_all

    slices = load_all()
    run_tag = args.run_tag or RUN_TAG
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else RUNS
    arith_path = (out_dir / "pre_call_arithmetic.json") if args.out_dir else ARITH

    attempts = []
    for name in [d.strip() for d in args.datasets.split(",") if d.strip()]:
        for it in slices.get(name, []):
            reps = args.repeats or it["repeats"]
            attempts += [(it, r) for r in range(1, reps + 1)]
    if args.limit:
        attempts = attempts[: args.limit]

    arithmetic = pre_call_arithmetic(len(attempts))
    arithmetic["run_tag"] = run_tag
    arithmetic["reader_prompt_variant"] = args.prompt
    arithmetic["slices"] = args.datasets
    arith_path.parent.mkdir(parents=True, exist_ok=True)
    arith_path.write_text(json.dumps(arithmetic, indent=2), encoding="utf-8")
    print("=== PRE-CALL ARITHMETIC (before any metered call) ===")
    print(json.dumps(arithmetic, indent=2))
    if args.dry_plan:
        print(f"\nplanned attempts: {len(attempts)}")
        return 0
    if arithmetic.get("verdict") == "BUDGET-GATE":
        return 3

    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_text = PROMPTS[args.prompt]
    h = Harness(run_tag)
    traces_f = (out_dir / "traces.jsonl").open("w", encoding="utf-8")
    preds_f = (out_dir / "predictions.jsonl").open("w", encoding="utf-8")
    try:
        for n, (item, rep) in enumerate(attempts, 1):
            try:
                rec = await run_attempt(h, item, rep, prompt_text, run_tag)
            except Exception as exc:
                rec = {
                    "run_tag": run_tag, "item_id": item["item_id"],
                    "dataset": item["dataset"], "repeat": rep,
                    "question": item["question"],
                    "question_sha256": item["question_sha256"],
                    "context_sha256": item["context_sha256"],
                    "attempt_error": f"{type(exc).__name__}: {exc}"[:200],
                    "arms": {},
                }
            traces_f.write(json.dumps(rec) + "\n")
            # Cross-arm draft identity is asserted here, at write time.
            for arm in ("off", "on"):
                a = rec.get("arms", {}).get(arm, {})
                preds_f.write(json.dumps({
                    "run_tag": run_tag, "item_id": rec["item_id"],
                    "dataset": rec["dataset"], "repeat": rep,
                    "shared_draft_hash": rec.get("shared_draft_hash", ""),
                    "arm": arm,
                    "answer": a.get("final_answer", ""),
                    "abstained": a.get("abstained", True),
                    "citations": a.get("citations", []),
                    "claims_grounded": a.get("claims_grounded", 0),
                    "claims_total": a.get("claims_total", 0),
                    "attempt_error": rec.get("attempt_error", rec.get("draft_error", "")),
                }) + "\n")
            if n % 20 == 0 or n == len(attempts):
                spent = sum(r["cost_usd"] for r in h.ledger_rows)
                print(f"[{n}/{len(attempts)}] spent=${spent:.6f}", flush=True)
    finally:
        traces_f.close()
        preds_f.close()
        write_ledger(h.ledger_rows)

    spent = round(sum(r["cost_usd"] for r in h.ledger_rows), 8)
    summary = {
        "run_tag": run_tag,
        "reader_prompt_variant": args.prompt,
        "n_attempts": len(attempts),
        "metered_calls": len(h.ledger_rows),
        "free_calls": sum(r["free_calls"] for r in h.ledger_rows),
        "paid_calls": sum(r["paid_calls"] for r in h.ledger_rows),
        "fallbacks": sum(r["fallbacks"] for r in h.ledger_rows),
        "failed_calls": sum(r["failed_calls"] for r in h.ledger_rows),
        "prompt_tokens": sum(r["prompt_tokens"] for r in h.ledger_rows),
        "completion_tokens": sum(r["completion_tokens"] for r in h.ledger_rows),
        "spent_usd": spent,
        "remaining_cap_usd": round(remaining_cap(), 6),
    }
    (out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
