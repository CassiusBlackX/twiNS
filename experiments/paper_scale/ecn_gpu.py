"""Original insertion order, four independent GPU worlds for each frozen arrival."""
import argparse
import json
from pathlib import Path
from scale import run
from reference_hashes import signature

p=argparse.ArgumentParser()
for arg in ('inputs','output','binary','reference'):p.add_argument(arg,type=Path)
a=p.parse_args()
frozen=json.loads(a.reference.read_text()) if a.reference.is_file() else None
for i in range(1,21):
    d=a.output.resolve()/str(i)
    d.mkdir(parents=True,exist_ok=True)
    worlds=d/'worlds.txt'
    keys=(10,20,30,40)
    worlds.write_text(''.join(f'flows={a.inputs.resolve()}/ecn/{i}.csv,tcp=dctcp,ecnK={k},queue=100,out={d}/{k}.csv\n' for k in keys))
    v=run([a.binary.resolve(),'--k=8','--spray=0',f'--worlds={worlds}'],d,'gpu',{},1800)
    flags=(d/'gpu.log').read_text()
    if v['rc'] or 'undecided-ties 0' not in flags:raise RuntimeError(d)
    for k in keys:
        rel=f'a{i}-o0-k{k}/flows.csv'
        expected=frozen['receipts/'+rel] if frozen else signature(a.reference/rel)
        if signature(d/f'{k}.csv')!=expected:raise ValueError((i,k))
    (d/'matched.json').write_text(json.dumps(dict(arrival=i,matched=keys)))
