from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

DATASET = (
    Path('/root/code/Super Rag') / 'datasets' / 'hotpotqa-full'
    / 'raw' / 'distractor' / 'validation-00000-of-00001.parquet'
)
OUT = Path('/root/code/Super Rag/knowledge-os/evals/benchmark_v1/index')
VUFEN = OUT / 'vectors.npy'
IDS = OUT / 'ids.json'
CKPT = OUT / 'checkpoint.json'
LEDGER = Path('/root/code/Super Rag/knowledge-os/evals/benchmark_v1/reports/budget_ledger.csv')
MANIFEST = OUT / 'index_manifest.json'
FUND = 1.95
RATE = 0.0000239
RESERVE = 0.20


async def main() -> None:
    print('loading dataset', flush=True)
    df = pd.read_parquet(DATASET)
    seen = {}
    order = []
    for _, row in df.iterrows():
        ctx = row['context']
        titles = list(ctx['title'])
        sents = list(ctx['sentences'])
        for title, sents_ in zip(titles, sents, strict=False):
            for p in (sents_ if isinstance(sents_, (list, tuple)) else [sents_]):
                text = str(p).strip()
                norm = ' '.join(text.split()).lower()
                title_norm = str(title).strip().lower()
                key = (title_norm, norm)
                if key not in seen:
                    seen[key] = len(order)
                    order.append({'title': str(title).strip(), 'text': text, 'key': list(key)})

    # validate count
    assert len(order) == 66635, len(order)
    print('unique paragraphs', len(order), flush=True)

    # embed provider
    import os
    os.environ.setdefault('PYTHONPATH', '')
    from models.adapters.openrouter_provider import OpenRouterProvider

    provider = OpenRouterProvider(call_site='h2-index')

    # resume
    start = 0
    vecs = None
    if CKPT.exists():
        ck = json.loads(CKPT.read_text())
        start = int(ck['next'])
        if VUFEN.exists():
            vecs = np.load(VUFEN, allow_pickle=False)
        else:
            raise RuntimeError('checkpoint exists but vectors.npy missing')
        if vecs.shape[0] != start:
            raise RuntimeError(f'vector count {vecs.shape[0]} != checkpoint {start}')
    if vecs is None:
        vecs = np.zeros((0, 768), dtype=np.float32)

    dim = None
    if vecs.shape[0] > 0:
        dim = int(vecs.shape[1])
    BATCH = 50
    CP = 5000
    i = start
    while i < len(order):
        batch = min(BATCH, len(order) - i)
        projected = (i + batch) * RATE * (1.0 + RESERVE)
        if projected > FUND:
            raise RuntimeError(f'budget overrun projection {projected:.4f} > {FUND}')
        texts = [order[j]['text'] for j in range(i, i + batch)]
        for _r in range(8):
            try:
                out = await provider.embed_documents(texts)
                break
            except Exception as e:
                if '429' in str(e):
                    await asyncio.sleep(8)
                    continue
                raise
        else:
            raise RuntimeError('429 exhausted')
        arr = np.asarray(out, dtype=np.float32)
        if dim is None:
            dim = int(arr.shape[1])
            if vecs.shape[0] == 0:
                vecs = np.zeros((0, dim), dtype=np.float32)
        if arr.shape[1] != dim:
            raise RuntimeError('embedding dimension changed')
        vecs = np.concatenate([vecs, arr], axis=0)
        i += batch
        spent = i * RATE
        if i % CP < BATCH or i == len(order):
            np.save(VUFEN, vecs)
            CKPT.write_text(json.dumps({'next': i, 'count': int(vecs.shape[0]), 'dim': dim}))
            with LEDGER.open('a') as f:  # noqa: ASYNC230
                f.write(f"h2-index,{i},{batch},{spent:.6f}\n")
            print('checkpoint', i, 'spent_est', round(spent, 4), flush=True)
    np.save(VUFEN, vecs)
    IDS.write_text(json.dumps([o['title'] for o in order], ensure_ascii=False))

    man = {
        'count': int(vecs.shape[0]),
        'embed_model': 'openrouter (pinned provider)',
        'chunking': 'one-vector-per-canonical-paragraph',
        'content_sha256': 'cb67996071b5b515389702dd77eb42d563a50ccf80c515bcd7e92be213467fd3',
        'index_checksum': hashlib.sha256(VUFEN.read_bytes()).hexdigest(),
        'dim': int(vecs.shape[1]),
    }
    MANIFEST.write_text(json.dumps(man, indent=2, ensure_ascii=False))
    print('DONE', vecs.shape, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
