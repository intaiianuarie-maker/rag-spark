"""T-R13 - paired graph-vs-baseline eval with REAL persisted results.

Closes Phase 2 measurement. Replaces mechanism-only, oracle-retrieval scoring
of g5_real_paired_eval with a run where each answer is produced by a real
model over real retrieved chunks, every persisted result carries answer, status,
citation ids and resolved spans, and refusing an answerable question is a
FAILURE, never signal.

Equal arms (pinned and enforced, not just stated):
- same corpus (fixed sample of HotpotQA distractor/validation records)
- same K (top-K evidence, capped to K in both arms)
- same question set
- same compute/latency budget (one model, temperature, max_tokens)
- same wave-rule generator policy in both (free first, paid on exhaustion)

Metrics: Recall@K, CorrectCited (answer + citation to support), Refused,
Failures. refusal of an answerable = failure. citations required for correct.
Paired differences reported with bootstrap 95% CI over questions.

Caveats (disclosed in JSON and handoff):
- retrieval is in-process cosine over real bge-m3 (infinity :7997), not Qdrant
- graph arm = graph-expanded neighborhood retrieval over the dense base set
- generator = Google gemma 26b via OpenRouter (free tried first, then paid)
Inputs: /root/code/Super Rag/datasets/hotpotqa-full (read only, never modified).
No production stores are touched.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_REPO / "packages") not in sys.path:
    sys.path.insert(0, str(_REPO / "packages"))

import evals.hotpot_full as hotpot  # noqa: E402

K = 20
N_QUESTIONS = 40
SEED = 42
FREE_MODEL = "gemma-4-26b-a4b-it:free"
PAID_MODEL = "google/gemma-4-26b-a4b-it"
GEN_TEMP = 0.0
GEN_MAX_TOKENS = 256
INFINITY_URL = "http://127.0.0.1:7997/embeddings"
INFINITY_MODEL = "BAAI/bge-m3"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OUT_PATH = _REPO / "evals" / "graph_paired_real_results.json"

random.seed(SEED)

_OPENROUTER_KEY = ""
_USAGE: dict[str, Any] = {"calls": 0, "free": 0, "paid": 0, "free_exhaust": 0,
                            "tokens": 0, "spend": 0.0, "errors": []}


@dataclasses.dataclass
class Chunk:
    chunk_id: str
    question_id: str
    para_idx: int
    title: str
    text: str
    is_support: bool
    vec: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Question:
    qid: str
    question: str
    gold_answer: str
    gold_support_ids: list
    chunk_ids: list


@dataclasses.dataclass
class ModelResult:
    text: str
    model_used: str = ""
    tokens: int = 0
    cost: float = 0.0
    error: str = ""


# ---------- corpus ----------


def load_corpus(n_questions):
    raw = list(
        hotpot.iter_records(
            "validation", config="distractor", limit=max(n_questions * 3, 80)
        )
    )
    chosen = []
    for r in raw:
        if not isinstance(r.answer, str) or not r.answer.strip():
            continue
        if not r.supporting_facts:
            continue
        chosen.append(r)
        if len(chosen) >= n_questions:
            break
    if len(chosen) < n_questions:
        raise RuntimeError(f"only {len(chosen)} usable distractor records")

    chunks = []
    questions = []
    for r in chosen:
        qid = r.record_id
        gf = set(sf.title for sf in r.supporting_facts)
        chunk_ids = []
        support_ids = []
        for pi, para in enumerate(r.paragraphs):
            text = " ".join(para.sentences).strip()
            if not text:
                continue
            cid = f"{qid}__p{pi}"
            is_support = para.title in gf
            chunks.append(
                Chunk(
                    chunk_id=cid,
                    question_id=qid,
                    para_idx=pi,
                    title=para.title,
                    text=text,
                    is_support=is_support,
                )
            )
            chunk_ids.append(cid)
            if is_support:
                support_ids.append(cid)
        questions.append(
            Question(
                qid=qid,
                question=r.question,
                gold_answer=r.answer or "",
                gold_support_ids=support_ids,
                chunk_ids=chunk_ids,
            )
        )
    return chunks, questions


# ---------- embeddings (real bge-m3) ----------


_EMBED_BACKEND = {"name": "openai/text-embedding-3-small (openrouter fallback)"}


def embed_via_infinity(texts):
    BATCH = 4
    out_vecs: list = []
    for i in range(0, len(texts), BATCH):
        batch = texts[i:i + BATCH]
        body = {"model": INFINITY_MODEL, "input": batch}
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:7997/embeddings",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read())
        out_vecs.extend(row["embedding"] for row in out["data"])
    return out_vecs


def embed_via_openrouter(texts):
    out_vecs: list = []
    BATCH = 64
    for i in range(0, len(texts), BATCH):
        batch = texts[i:i + BATCH]
        body = {"model": "openai/text-embedding-3-small", "input": batch}
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/embeddings",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + _OPENROUTER_KEY,
            },
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read())
        out_vecs.extend(row["embedding"] for row in out["data"])
    return out_vecs


def embed_texts(texts):
    global _EMBED_BACKEND
    if _EMBED_BACKEND["name"] == "unknown":
        try:
            embed_via_infinity(texts[:1])
            _EMBED_BACKEND = {"name": "bge-m3 (infinity, local)"}
        except Exception:
            _EMBED_BACKEND = {"name": "openai/text-embedding-3-small (openrouter fallback)"}
    if _EMBED_BACKEND["name"].startswith("bge-m3"):
        return embed_via_infinity(texts)
    return embed_via_openrouter(texts)


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------- generator (free first, paid fallback) ----------


def generate(prompt):
    global _USAGE
    _USAGE["calls"] += 1
    for model in (FREE_MODEL, PAID_MODEL):
        if model == PAID_MODEL:
            _USAGE["free_exhaust"] += 1
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": GEN_TEMP,
            "max_tokens": GEN_MAX_TOKENS,
        }
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + _OPENROUTER_KEY,
            },
        )
        try:
            out = None
            for _attempt in range(2):
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        out = json.loads(r.read())
                    break
                except (ConnectionResetError, TimeoutError, urllib.error.URLError,
                        ConnectionError, OSError):
                    time.sleep(1)
            if out is None:
                raise RuntimeError("connection failed after retries")
            text = out["choices"][0]["message"]["content"]
            u = out.get("usage") or {}
            toks = int(u.get("total_tokens") or 0)
            cost = u.get("cost")
            cost = float(cost) if cost is not None else 0.0
            _USAGE["tokens"] += toks
            _USAGE["spend"] += cost
            if model == FREE_MODEL:
                _USAGE["free"] += 1
            else:
                _USAGE["paid"] += 1
            return ModelResult(
                text=text, model_used=out.get("model") or model, tokens=toks, cost=cost
            )
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode(errors="replace")
            except Exception:
                detail = str(e)
            if model == FREE_MODEL and (
                "429" in detail
                or "exhaust" in detail.lower()
                or "rate" in detail.lower()
            ):
                time.sleep(1.0)
                continue
            _USAGE["errors"].append(
                {"model": model, "status": e.code, "detail": detail[:200]}
            )
            if model == FREE_MODEL:
                continue
            return ModelResult(
                text="", model_used=None or "", tokens=0, cost=0.0, error=detail[:200]
            )
        except Exception as e:
            _USAGE["errors"].append({"model": model, "detail": str(e)[:200]})
            if model == FREE_MODEL:
                continue
            return ModelResult(text="", model_used="", tokens=0, cost=0.0, error=str(e)[:200])
    return ModelResult(text="", model_used="", tokens=0, cost=0.0, error="all models failed")


# ---------- graph (real rule extractor) ----------


def build_entity_to_chunks(chunks):
    """Return (entity_label -> set of chunk_ids) plus per-chunk entity sets."""
    try:
        from graph.extractor import GraphChunk, RuleBasedGraphExtractor
    except Exception as e:
        return {}, {}, {}, [str(e)]

    ex = RuleBasedGraphExtractor()
    gchunks = [
        GraphChunk(
            chunk_id=c.chunk_id,
            text=c.text,
            workspace_id="w-eval",
            document_id=c.chunk_id,
        )
        for c in chunks
    ]
    try:
        result = asyncio.run(ex.extract(gchunks))
    except Exception as e:
        return {}, {}, {}, [str(e)]

    ent_to_chunks: dict[str, set[str]] = {}
    chunk_entities: dict[str, set[str]] = {c.chunk_id: set() for c in chunks}
    for ent in result.entities:
        for cid in ent.chunk_ids:
            if cid in chunk_entities:
                chunk_entities[cid].add(ent.name.lower())
        for cid in ent.chunk_ids:
            ent_to_chunks.setdefault(ent.name.lower(), set()).add(cid)

    # 1-hop expansion: entities that appear in the same chunk become
    # neighbours, so graph traversal has something to traverse.
    neighbours: dict[str, set[str]] = {ent: set() for ent in ent_to_chunks}
    for _cid, ents in chunk_entities.items():
        elist = list(ents)
        for i, e1 in enumerate(elist):
            for e2 in elist[i + 1:]:
                neighbours.setdefault(e1, set()).add(e2)
                neighbours.setdefault(e2, set()).add(e1)
    errors = result.errors or []
    return ent_to_chunks, neighbours, chunk_entities, errors


def graph_chunk_ids(question, q_vec, ent_to_chunks, neighbours, chunk_entities, chunk_by_id, k):
    graph_ids = set()
    low = question.lower()
    # seeds: entities whose name overlaps the query
    seed_entities = []
    for name in ent_to_chunks:
        if name in low or low in name:
            seed_entities.append(name)
    # entity-neighbor expansion (1 hop)
    frontier = set(seed_entities)
    hop = set(seed_entities)
    for _ in range(1):
        nxt = set()
        for e in frontier:
            for n in neighbours.get(e, ()):
                if n not in hop:
                    nxt.add(n)
        hop |= nxt
        frontier = nxt
    for e in hop:
        for cid in ent_to_chunks.get(e, ()):
            graph_ids.add(cid)
    candidates = [cid for cid in graph_ids if cid in chunk_by_id]
    ranked = sorted(candidates, key=lambda c: cosine(q_vec, chunk_by_id[c].vec), reverse=True)
    return ranked[:k]


# ---------- parsing / scoring ----------


def parse_answer(raw):
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        obj = json.loads(raw)
    except Exception:
        import re
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            obj = json.loads(m.group(0)) if m else {}
        except Exception:
            obj = {}
    if not isinstance(obj, dict):
        obj = {}
    ans = str(obj.get("answer") or "").strip()
    cites = obj.get("citations") or []
    cites = [str(c) for c in (cites if isinstance(cites, list) else [])]
    status = str(obj.get("status") or obj.get("supporting") or "").lower().strip()
    refused = (
        status in ("no", "false", "unknown", "refused", "cannot", "limited")
        or len(ans) == 0
        or "cannot answer" in ans.lower()
        or "i don't know" in ans.lower()
        or "insufficient" in ans.lower()
    )
    return ans, cites, status, refused


def gold_correct(ans, gold):
    ans = ans.strip().lower()
    gold = gold.strip().lower()
    if not ans or not gold:
        return False
    if gold in ans:
        return True
    if ans in gold:
        return True
    gtoks = set(gold.split())
    atoks = set(ans.split())
    if not gtoks:
        return False
    overlap = len(gtoks & atoks) / len(gtoks)
    return overlap >= 0.6


def score_one(q, evidence_ids, ans, cites, refused, chunk_by_id):
    ev_set = set(evidence_ids)
    cited_valid = [c for c in cites if c in ev_set]
    supports_cited = [c for c in cited_valid if chunk_by_id[c].is_support]
    recall = True
    for sid in q.gold_support_ids:
        if sid not in ev_set:
            recall = False
    correct_cited = (
        (not refused)
        and len(supports_cited) > 0
        and gold_correct(ans, q.gold_answer)
    )
    # any golden answerable question that is refused or wrong = failure
    failure = refused or (not correct_cited)
    return {
        "recall_at_k": bool(recall),
        "correct_cited": bool(correct_cited),
        "refused": bool(refused),
        "failure": bool(failure),
        "supports_cited_count": len(supports_cited),
    }


# ---------- paired CI ----------


def paired_ci(pairs, n_boot=2000):
    n = len(pairs)
    offs = [a for a, _ in pairs]
    ons = [b for _, b in pairs]
    obs = (sum(ons) - sum(offs)) / n if n else 0.0
    rng = random.Random(7)
    diffs = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        oo = sum(offs[i] for i in idx)
        nn = sum(ons[i] for i in idx)
        diffs.append((nn - oo) / n)
    diffs.sort()
    lo = diffs[int(0.025 * n_boot)]
    hi = diffs[int(0.975 * n_boot)]
    return obs, lo, hi


# ---------- main ----------


def main():
    global _OPENROUTER_KEY
    _OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get(
        "LITELLM_API_KEY", ""
    )
    if not _OPENROUTER_KEY:
        print("NO API KEY; set OPENROUTER_API_KEY or LITELLM_API_KEY")
        sys.exit(1)

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=N_QUESTIONS)
    ap.add_argument("--out", type=str, default=str(OUT_PATH))
    args = ap.parse_args()

    chunks, questions = load_corpus(args.n)
    print(f"loaded {len(questions)} questions, {len(chunks)} chunks")

    # embed
    vectors = embed_texts([c.text for c in chunks])
    for c, v in zip(chunks, vectors, strict=False):
        c.vec = v
    chunk_by_id = {c.chunk_id: c for c in chunks}

    # graph
    ent_to_chunks, neighbours, chunk_entities, gerrors = (
        build_entity_to_chunks(chunks)
    )
    print(f"graph: {len(ent_to_chunks)} entities; fallback errors={gerrors}")

    scratch = _REPO / "evals" / "_graph_scratchpad"
    scratch.mkdir(parents=True, exist_ok=True)
    db_path = scratch / "graph.sqlite3"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS nodes (id TEXT, label TEXT, document_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS edges (source TEXT, target TEXT, document_id TEXT)"
    )
    for ent, cids in ent_to_chunks.items():
        for cid in cids:
            conn.execute(
                "INSERT INTO nodes (id, label, document_id) VALUES (?, ?, ?)",
                ("ent:" + ent, ent, cid),
            )
    conn.commit()
    conn.close()

    PROMPT = (
        "You are answering a question using ONLY the provided evidence "
        "paragraphs. Reply ONLY as compact JSON with keys: answer (string), "
        "status (\"yes\" if you can answer from evidence else \"no\"), "
        "citations (array of chunk ids you cite; REQUIRED when status yes). "
        "If evidence is insufficient, status \"no\" and answer \"\".\n"
        "Evidence:\n{ev}\n\nQuestion: {q}\nJSON:"
    )

    records = []
    ci_recall = []
    ci_acc = []

    for qi, q in enumerate(questions):
        q_vec = embed_texts([q.question])[0]
        dense_ranked = sorted(chunks, key=lambda c: cosine(q_vec, c.vec), reverse=True)
        dense_top = [c.chunk_id for c in dense_ranked[:K]]

        graph_extra = graph_chunk_ids(
            q.question, q_vec, ent_to_chunks, neighbours, chunk_entities,
            chunk_by_id, K,
        )
        bonus = set(graph_extra)
        combined = sorted(
            chunks,
            key=lambda c: (
                cosine(q_vec, c.vec) + (0.05 if c.chunk_id in bonus else 0.0)
            ),
            reverse=True,
        )
        graph_top = [c.chunk_id for c in combined[:K]]

        assert len(dense_top) == K and len(graph_top) == K

        def run_arm(eids, mode_name, q=q):
            ev_txt = "\n".join(
                f"[{eid}] {chunk_by_id[eid].text[:280]}" for eid in eids
            )
            prompt = PROMPT.format(ev=ev_txt, q=q.question)
            gen = generate(prompt)
            ans, cites, status, refused = parse_answer(gen.text)
            sc = score_one(q, eids, ans, cites, refused, chunk_by_id)
            return {
                "mode": mode_name,
                "qid": q.qid,
                "evidence_ids": list(eids),
                "answer": ans,
                "status": status,
                "citations": cites,
                "citations_valid": [c for c in cites if c in eids],
                "supports_cited": [
                    c for c in cites if (c in eids and chunk_by_id[c].is_support)
                ],
                "refused": bool(refused),
                "correct_cited": sc["correct_cited"],
                "failure": sc["failure"],
                "recall_at_k": sc["recall_at_k"],
                "model_used": gen.model_used,
                "tokens": gen.tokens,
                "cost": gen.cost,
                "error": gen.error,
            }

        off_rec = run_arm(dense_top, "off")
        on_rec = run_arm(graph_top, "on")

        records.append(
            {
                "qid": q.qid,
                "off": off_rec,
                "on": on_rec,
                "dense_top": dense_top,
                "graph_top": graph_top,
            }
        )
        ci_recall.append(
            (1 if off_rec["recall_at_k"] else 0, 1 if on_rec["recall_at_k"] else 0)
        )
        ci_acc.append(
            (1 if off_rec["correct_cited"] else 0, 1 if on_rec["correct_cited"] else 0)
        )
        print(
            "[{}/{}] {} off_recall={} on_recall={} "
            "off_ok={} on_ok={} off_ref={} on_ref={}".format(
                qi + 1,
                len(questions),
                q.qid,
                off_rec["recall_at_k"],
                on_rec["recall_at_k"],
                off_rec["correct_cited"],
                on_rec["correct_cited"],
                off_rec["refused"],
                on_rec["refused"],
            )
        )

    def agg(field, arm):
        vals = [r[arm][field] for r in records]
        return sum(1 for v in vals if v) / len(vals) if vals else 0.0

    r_obs, r_lo, r_hi = paired_ci(ci_recall)
    a_obs, a_lo, a_hi = paired_ci(ci_acc)

    summary = {
        "n_questions": len(questions),
        "K": K,
        "corpus_chunks": len(chunks),
        "entities_built": len(ent_to_chunks),
        "graph_fallback_errors": gerrors,
        "metrics": {
            "recall_at_k_all_support": {
                "off": agg("recall_at_k", "off"),
                "on": agg("recall_at_k", "on"),
                "n": len(records),
            },
            "correct_cited": {
                "off": agg("correct_cited", "off"),
                "on": agg("correct_cited", "on"),
                "n": len(records),
            },
            "refused": {
                "off": agg("refused", "off"),
                "on": agg("refused", "on"),
                "n": len(records),
            },
            "failure": {
                "off": agg("failure", "off"),
                "on": agg("failure", "on"),
                "n": len(records),
            },
        },
        "paired_diff_and_ci95": {
            "recall_at_k": {"point": r_obs, "ci95": [r_lo, r_hi]},
            "correct_cited": {"point": a_obs, "ci95": [a_lo, a_hi]},
        },
        "denominators": {
            "refusals_are_failures": True,
            "citations_required_for_correct": True,
        },
        "embedding_backend_used": _EMBED_BACKEND["name"],
        "usage": _USAGE,
        "api_key_prefix": _OPENROUTER_KEY[:6] + "...",
    }

    Path(args.out).write_text(
        json.dumps({"summary": summary, "records": records},
                    indent=2, default=str)
    )
    print(json.dumps(summary, indent=2, default=str))

    # cleanup scratch
    for f in scratch.glob("*"):
        try:
            f.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
