#!/usr/bin/env python3
"""At a completed latency sample, checkpoint own campaign to generate new HPCC references.
Any following partial attempt is retained as preempted; completed timings are never changed.
"""
import csv,hashlib,json,os,subprocess,time
from pathlib import Path
ROOT=Path.home()/'Videos/twins-paper-evidence-20261116';OUT=ROOT/'out/paper-evidence-20261116';EXP=OUT/'export';target=EXP/'attempts/latency-r1-gpt3_13b_full_hpn128-p0/result.json'
while not target.exists():time.sleep(1)
partials=[p for p in (EXP/'attempts').iterdir() if (p/'started.json').exists() and not (p/'result.json').exists()]
subprocess.run(['systemctl','--user','stop','twins-evidence-campaign'],check=True)
for line in subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines():
 if line.startswith('twins-evidence-'):subprocess.run(['docker','stop','-t','3',line],check=False)
pre=[]
for p in partials:
 if (p/'result.json').exists() and json.loads((p/'result.json').read_text()).get('accepted'):continue
 pre.append(p.name);(p/'preempted.json').write_text(json.dumps(dict(utc_ns=time.time_ns(),reason='checkpoint after completed sample for new reference generation; not a formal timing sample')))
 dest=EXP/'preempted'/p.name;dest.parent.mkdir(exist_ok=True)
 if dest.exists():dest=dest.with_name(dest.name+'-'+str(time.time_ns()))
 p.rename(dest)
refroot=EXP/'growth-references';refroot.mkdir(exist_ok=True);old=Path.home()/'Videos/twins-audit';bin=Path.home()/'Videos/netsim/hpcc/simulation/build/scratch/rdma-ref';lib=bin.parents[1]
source=old/'data/rdma/k8_perm.csv';topo=old/'data/topo/fattree8.txt';cfg=old/'validation/hpcc/hpcc.cfg'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
with source.open() as f:rows=list(csv.reader(f))
records=[]
for scale in (1,2,4,8):
 d=refroot/f'x{scale}';d.mkdir(exist_ok=True);flow=d/'flows.csv'
 with flow.open('w') as f:
  w=csv.writer(f);w.writerow(rows[0]);w.writerows([r[:2]+[str(int(r[2])*scale)]+r[3:] for r in rows[1:]])
 cmd=['taskset','-c','0',str(bin),str(cfg),'--topo='+str(topo),'--flows='+str(flow),'--out='+str(d/'reference.fct'),'CC_MODE=3','BUFFER_SIZE=1']
 with (d/'process.log').open('w') as f:
  start=time.perf_counter_ns()
  try:rc=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,timeout=3600,env={**os.environ,'LD_LIBRARY_PATH':str(lib)}).returncode
  except subprocess.TimeoutExpired:rc=124
  wall=time.perf_counter_ns()-start
 fct=d/'reference.fct';n=sum(1 for _ in fct.open()) if fct.exists() else 0
 v=dict(scale=scale,command=cmd,rc=rc,wall_ns=wall,flow_sha256=sha(flow),reference_sha256=sha(fct) if fct.exists() else None,reference_rows=n,expected_rows=len(rows)-1,binary_sha256=sha(bin),config_sha256=sha(cfg),topology_sha256=sha(topo),accepted=rc==0 and n==len(rows)-1)
 (d/'result.json').write_text(json.dumps(v,indent=2)+'\n');records.append(v)
(refroot/'summary.json').write_text(json.dumps(records,indent=2)+'\n')
(EXP/'checkpoint-growth.json').write_text(json.dumps(dict(utc_ns=time.time_ns(),preempted=pre,reference_results=records),indent=2)+'\n')
subprocess.run(['systemctl','--user','reset-failed','twins-evidence-campaign'],check=False)
subprocess.run(['systemd-run','--user','--unit=twins-evidence-campaign','--property=MemoryMax=80G','--working-directory='+str(ROOT),'/usr/bin/python3','experiments/paper_evidence/campaign.py','all'],check=True)
