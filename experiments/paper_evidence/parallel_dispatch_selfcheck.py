#!/usr/bin/env python3
"""Exercise the real18-batch dispatcher on small sleeping subprocesses; never model/GPU data."""
import contextlib,io,json,sys,tempfile
from pathlib import Path
import parallel_batches as p
b=p.b;c=p.c
with tempfile.TemporaryDirectory(prefix='twins-batch-dispatch-',dir='/tmp') as tmp:
 root=Path(tmp);c.EXPORT=root/'export';c.EXPORT.mkdir();p.P=c.EXPORT/'parallel';p.P.mkdir();b.BROOT=c.EXPORT/'batches';b.BROOT.mkdir();b.RAW=root/'raw';b.RAW.mkdir();p.BUDGET=4;p.GPU_HOST=1;p.os.sched_setaffinity=lambda *args:None
 c.JOBS=[dict(case='one',old_simai_s=2),dict(case='two',old_simai_s=1)];b.cap_manifest=lambda:dict(jobs={j['case']:dict(cap_gib=1) for j in c.JOBS});c.snapshot=lambda:dict(utc_ns=0)
 def prepare(trial,j,engine,cpu,policy):
  d=trial/j['case'];d.mkdir();raw=b.RAW/(trial.name+'-'+j['case']);raw.mkdir();return dict(j=j,d=d,raw=raw,container='',v=dict(case=j['case'],engine='p0',command=[sys.executable,'-c','import time;time.sleep(.05)','--cpuset-cpus','0']))
 b.prepare=prepare;b.validate=lambda t:dict(case=t['j']['case'],accepted=True)
 with contextlib.redirect_stdout(io.StringIO()):p.run()
 events=[json.loads(l) for l in (p.P/'batch-dispatch.jsonl').read_text().splitlines()];checks=dict(all18_batches_returned=len(list(b.BROOT.glob('*.json')))==18,global_memory_never_above_budget=True,allocated_cores_never_shared=True,p16_always_exclusive=True,gpu_never_shared=True,replicas_actually_overlap=False)
 for x in events:
  vs=list(x['allocations'].values());checks['global_memory_never_above_budget'] &= sum(v['memory'] for v in vs)<=4;used=set()
  for v in vs:checks['allocated_cores_never_shared'] &= not(used & set(v['cores']));used.update(v['cores'])
  checks['gpu_never_shared'] &= sum(v['gpu'] for v in vs)<=1
  if any(v['exclusive'] for v in vs):checks['p16_always_exclusive'] &= len(vs)==1
  checks['replicas_actually_overlap'] |= len(vs)>1
 print(checks);assert all(checks.values());out=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/receipts/parallel-dispatch-selfcheck.json');out.write_text(json.dumps(dict(checks=checks,passed=True,scope='real dispatcher and real sleeping subprocesses, not model performance'),indent=2))
