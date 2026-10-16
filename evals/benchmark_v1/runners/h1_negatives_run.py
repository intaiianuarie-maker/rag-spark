import asyncio
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path("/root/code/Super Rag/knowledge-os")
sys.path.insert(0,str(ROOT/'packages'))
sys.path.insert(0,str(ROOT/'apps/api'))
sys.path.insert(0,str(ROOT/'evals/benchmark_v1/runners'))
from h1_run import DRAFT_PROMPT, Harness  # noqa: E402

dataset = (
    Path("/root/code/Super Rag")
    / "datasets"
    / "hotpotqa-full"
    / "raw"
    / "distractor"
    / "validation-00000-of-00001.parquet"
)
df=pd.read_parquet(dataset).set_index('id')
neg=json.load(open(ROOT/'evals/benchmark_v1/datasets/negative_manifest.json'))
OUT=ROOT/'evals/benchmark_v1/runs/h1_negatives'
OUT.mkdir(parents=True,exist_ok=True)
async def main():
    h=Harness()
    traces=[]
    for nid in neg['ids']:
        row=df.loc[nid]
        q=row['question']
        snapdir=ROOT/'evals'/'benchmark_v1'/'datasets'/'negatives'/nid
        paras=[]
        for p in sorted(snapdir.glob('*.txt')):
            paras.append({'title':p.stem,'text':p.read_text()})
        ctx=h.context_block(paras)
        draft,_,_=await h.generate(DRAFT_PROMPT.format(ctx=ctx,q=q), f"h1_neg_{nid}")
        abstained = bool(draft.get('abstained', False))
        traces.append({'id':nid,'question':q,'abstained':abstained,'draft':draft})
    (OUT/'traces.jsonl').write_text('\n'.join(json.dumps(t) for t in traces)+'\n')
    print('done',len(traces))
asyncio.run(main())
