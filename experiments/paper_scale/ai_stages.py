"""Generate/run buffer-paired two-stage AllToAll on a common live workload."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

p=argparse.ArgumentParser()
p.add_argument('root',type=Path)
p.add_argument('mode',choices=('prepare','reference','gpu'))
p.add_argument('--exe',type=Path)
p.add_argument('--q3cap',type=int,default=65536)
a=p.parse_args()
root=a.root.resolve()
if a.mode=='prepare':
    single=(root/'loss/workload.txt').read_text().splitlines()
    first=single[2]
    for case,buffer in [('large-buffer',64),('small-buffer',1)]:
        d=root/case
        d.mkdir(exist_ok=True)
        (d/'workload.txt').write_text('\n'.join([single[0],'2',first,first.replace('coll_layer','next_collective')])+'\n')
        (d/'config').write_text((root/'loss/config').read_text().replace('BUFFER_SIZE 1\n',f'BUFFER_SIZE {buffer}\n'))
        (d/'ref.flows').write_text('0\n')
else:
    for case in ('large-buffer','small-buffer'):
        d=root/case
        cmd=[str(a.exe.resolve()),'config']
        env={**os.environ,'AI_TOPO':'../simai.topo','AI_WORKLOAD':'workload.txt','IRN_BDP_BYTES':'104000'}
        if a.mode=='gpu':
            cmd+=['--topo=../simai.topo','--astra=workload.txt','--out=gpu.fct','--dag-out=gpu.dag','IRN_BDP=104000',f'--q3cap={a.q3cap}']
        start=time.monotonic_ns()
        with (d/(a.mode+'.log')).open('w') as f:
            rc=subprocess.run(cmd,cwd=d,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=900).returncode
        (d/(a.mode+'.json')).write_text(json.dumps(dict(rc=rc,command=cmd,wall_ns=time.monotonic_ns()-start)))
        (d/(a.mode+'.rc')).write_text(str(rc)+'\n')
