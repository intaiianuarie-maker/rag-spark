
"""G5 hotpot corpus loader - frozen + enforced (T-075 pattern)."""
import hashlib
import json
import pathlib

import pyarrow.parquet as pq

PIN_PATH = pathlib.Path(__file__).parent / "g5_hotpot_pin.json"

def load_g5_corpus() -> list[dict]:
    pin = json.loads(PIN_PATH.read_text())
    base = pathlib.Path("/root/code/Super Rag/datasets/rag-eval")
    rel = pin["parquet_path"].replace("datasets/rag-eval/", "")
    parquet_path = base / rel
    # Verify hash
    h = hashlib.sha256(open(parquet_path, 'rb').read()).hexdigest()
    if h != pin["parquet_sha256"]:
        raise RuntimeError(f"G5 corpus drift: {h} != {pin['parquet_sha256']}")
    pf = pq.ParquetFile(str(parquet_path))
    table = pf.read()
    # Return selected rows
    out=[]
    for idx in pin["selected_indices"]:
        out.append({
            "row": idx,
            "question": table.column("question")[idx].as_py(),
            "documents": table.column("documents")[idx].as_py(),
            "sentences": table.column("documents_sentences")[idx].as_py(),
            "gold_keys": table.column("all_relevant_sentence_keys")[idx].as_py(),
            "response": table.column("response")[idx].as_py(),
        })
    return out
