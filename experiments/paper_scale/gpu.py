"""Run only the RX/CUDA part of a fixed reference pair (no CPU simulation)."""
import argparse,json,os,subprocess,time
from pathlib import Path
p=argparse.ArgumentParser()
for k in ('inputs','out','build'):p.add_argument(k,type=Path)
p.add_argument('--modes',nargs='+',default=['spread','incast'])
p.add_argument('--hosts',nargs='+',type=int,default=[128,256,512,1024])
a=p.parse_args()
for h in a.hosts:
    for m in a.modes:
        src=a.inputs.resolve()/str(h);d=a.out.resolve()/m/str(h);d.mkdir(parents=True,exist_ok=True)
        if (d/'gpu.json').exists():continue
        tcp=m in ('uniform','heterogeneous')
        cmd=([a.build.resolve()/'twins','--topo=graph',f'--topofile={src}/{m}.graph','--tcp=dctcp','--ecnK=30',f'--flows={src}/tcp/flows.csv'] if tcp else
             [a.build.resolve()/'twins-rdma',src/m/'config',f'--topo={src}/uniform.graph',f'--flows={src}/{m}/flows.csv','IRN_BDP=156000'])
        cmd.append('--out=gpu.fct')
        with (d/'gpu.log').open('w') as log:
            start=time.monotonic_ns()
            try:rc=subprocess.run(cmd,cwd=d,stdout=log,stderr=subprocess.STDOUT,timeout=1800).returncode
            except subprocess.TimeoutExpired:rc=124
        (d/'gpu.json').write_text(json.dumps(dict(command=list(map(str,cmd)),rc=rc,wall_ns=time.monotonic_ns()-start)))
