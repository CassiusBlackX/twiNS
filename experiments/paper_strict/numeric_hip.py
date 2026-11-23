"""Run the same archived FMA comparison inputs through the HIP candidate."""
import fcntl
import json
from pathlib import Path
import sys
import time

import numeric_libs as study

out=Path(sys.argv[1]).resolve()
ws=Path(sys.argv[2]).resolve()
out.mkdir(parents=True,exist_ok=False)
base=out.parent
study.INPUT=ws/'twins-paper-scale/out/campaign/inputs/128'
bin=base/'bda-hip/build/twins'
deadline=time.monotonic()+10800
while time.monotonic()<deadline:
    dirs=sorted((base/'numeric_libs').glob('20*'))
    paths=[d for d in dirs if (d/'cpu-summary.json').is_file()]
    if paths and bin.is_file():
        remote=paths[-1]
        groups=json.loads((remote/'cpu-summary.json').read_text())
        if len(groups)==4 and all((remote/'cases'/k/'off/output.fct').is_file() for k in groups):
            break
    time.sleep(15)
else:
    raise RuntimeError('rebuilt CPU references were not returned')
flow_hash=study.sha(study.INPUT/'tcp/flows.csv')
if flow_hash!='65cbef1a1b94af8bd2645558c4df8a3a018b80e0acd36a48d73789a798ff4acc':
    raise ValueError(f'flow mismatch {flow_hash}')

with Path('/tmp/specdes-pfc-and-replay-hip.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    result={}
    for case in groups:
        graph,kind=case.split('/')
        p=out/case
        argv=study.invoke(p,bin,remote/'inputs'/f'{graph}.graph',{'dctcp':'Dctcp','cubic':'Cubic'}[kind])
        row=dict(run=argv,compare=study.compare(out,remote/'cases'/case/'off',p),source_input_sha256=flow_hash)
        result[case]=row
        (out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
