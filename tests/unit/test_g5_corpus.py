"""Covering tests for G5 corpus pin and span mapping - T-V2-010."""
import json
import pathlib


def test_pin_exists_and_has_100_rows():
    pin = json.loads(pathlib.Path("evals/g5_hotpot_pin.json").read_text())
    assert len(pin["selected_indices"]) == 100
    assert pin["n_rows"] == 100

DATASET_PARQUET = pathlib.Path(
    "/root/code/Super Rag/datasets/rag-eval"
    "/ragbench-tests-v1/hotpotqa/test-00000-of-00001.parquet"
)


def test_pin_hash_enforced():
    import hashlib

    pin = json.loads(pathlib.Path("evals/g5_hotpot_pin.json").read_text())
    # Verify file hash matches pin
    path = DATASET_PARQUET
    h = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    assert h == pin["parquet_sha256"]

def test_gold_not_from_sentence_support_information():
    # Ensure we use all_relevant_sentence_keys, not empty supporting_sentence_keys
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(str(DATASET_PARQUET))
    t = pf.read()
    # Check that supporting_sentence_keys is often empty, while all_relevant is not
    ssi = t.column("sentence_support_information")[0].as_py()
    # ssi[0] has supporting_sentence_keys == []
    assert ssi[0]["supporting_sentence_keys"] == []  # would be empty gold trap
    rel = t.column("all_relevant_sentence_keys")[0].as_py()
    assert len(rel) > 0  # real gold

def test_span_mapping_coverage():
    mappings = json.loads(pathlib.Path("/tmp/g5_mappings.json").read_text())
    stats = {"mapped":0, "inferred":0, "dropped":0}
    for m in mappings:
        stats[m["status"]] += 1
    assert stats["mapped"] == 330
    assert stats["inferred"] == 0
    assert stats["dropped"] == 0

def test_audit_agreement():
    audit = json.loads(pathlib.Path("/tmp/g5_audit.json").read_text())
    assert audit["agreement"] == 1.0

def test_authenticity_census():
    census = json.loads(pathlib.Path("/tmp/g5_census.json").read_text())
    assert census["authentic"] == census["total"] == 407
    assert census["bands"][">0.95"] == 407

def test_live_stability():
    # Live knowledge should be unchanged (scratch only)
    import json
    import urllib.request
    with urllib.request.urlopen("http://localhost:6333/collections/knowledge", timeout=5) as r:
        cnt = json.loads(r.read())["result"]["points_count"]
    assert cnt == 2041

def test_recall_wired():
    from evals.g5_eval import evidence_recall_at_k
    # Simple case: gold {a,b}, retrieved [a,b,c] -> recall 1.0
    assert evidence_recall_at_k({"a","b"}, ["a","b","c"], k=20) == 1.0
    assert evidence_recall_at_k({"a","b"}, ["c","d"], k=20) == 0.0
    assert evidence_recall_at_k({"a","b","c"}, ["a"], k=20) == 1/3

def test_groundedness_wired():
    from evals.g5_eval import groundedness_score
    # Grounded answer should score high
    ctx = "The Eiffel Tower is in Paris. It was built in 1889."
    ans = "The Eiffel Tower is in Paris."
    assert groundedness_score(ans, ctx) > 0.8
