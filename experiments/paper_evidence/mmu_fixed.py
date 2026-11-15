#!/usr/bin/env python3
import hashlib,json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];OUT=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/local/mmu')
while subprocess.run(['systemctl','--user','is-active','--quiet','twins-evidence-residuals']).returncode==0:time.sleep(10)
results=[]
for label,opts in [('fixed-safe',[]),('fixed-serial',['--serial']),('fixed-window',['--win=4'])]:
 d=OUT/label;d.mkdir(exist_ok=True)
 cmd=[str(ROOT/'build-mmu/twins-rdma'),str(OUT/'mixed.cfg'),'--topo='+str(OUT/'low_headroom.topo'),'--flows='+str(OUT/'one.csv'),'--out='+str(d/'fct'),'BUFFER_SIZE=1','SIMULATOR_STOP_TIME=0.005','CC_MODE=3',*opts]
 with (d/'process.log').open('w') as f:
  start=time.perf_counter_ns()
  try:rc=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,timeout=60,cwd=ROOT).returncode
  except subprocess.TimeoutExpired:rc=124
  wall=time.perf_counter_ns()-start
 v=dict(label=label,command=cmd,rc=rc,wall_ns=wall,binary_sha256=hashlib.sha256((ROOT/'build-mmu/twins-rdma').read_bytes()).hexdigest(),fct=(d/'fct').read_text() if (d/'fct').exists() else None,log=(d/'process.log').read_text(errors='replace'))
 (d/'result.json').write_text(json.dumps(v,indent=2));results.append(v);print(json.dumps(v),flush=True)
(OUT/'network-fixed.json').write_text(json.dumps(results,indent=2))
