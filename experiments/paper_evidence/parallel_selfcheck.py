#!/usr/bin/env python3
"""Real subprocess checks for shrinking quotas and disjoint batch allocations."""
import concurrent.futures as cf,json,sys,tempfile,threading,time
from pathlib import Path
import batches as b
with tempfile.TemporaryDirectory(prefix='twins-parallel-check-',dir='/tmp') as tmp:
 root=Path(tmp);b.BROOT=root/'export';b.BROOT.mkdir();b.RAW=root/'raw';b.RAW.mkdir();b.c.JOBS=[dict(case='large',old_simai_s=2),dict(case='small',old_simai_s=1)]
 def prepare(trial,j,engine,cpu,policy):
  d=trial/j['case'];d.mkdir();raw=b.RAW/(trial.name+'-'+j['case']);raw.mkdir();return dict(j=j,d=d,raw=raw,container='',v=dict(case=j['case'],engine='p0',command=[sys.executable,'-c','import time;time.sleep(.15)','--cpuset-cpus','0']))
 b.prepare=prepare;b.validate=lambda t:dict(case=t['j']['case'],accepted=True);b.c.snapshot=lambda:dict(utc_ns=time.time_ns())
 policy=dict(jobs=dict(large=dict(cap_gib=2),small=dict(cap_gib=1)))
 updates=[];mx=threading.Lock()
 def update(mem,cores):
  with mx:updates.append((mem,list(cores),time.perf_counter_ns()))
 with cf.ThreadPoolExecutor(2) as pool:
  fs=[pool.submit(b.batch,'replica'+str(i),'simai',1,policy,[i],dict(test=True),update) for i in (0,1)]
  for f in fs:f.result()
 xs=[json.loads((b.BROOT/('replica'+str(i)+'.json')).read_text()) for i in (0,1)]
 overlap=max(x['start_utc_ns'] for x in xs)<min(x['start_utc_ns']+x['makespan_ns'] for x in xs)
 checks=dict(real_batches_overlap=overlap,core_pools_disjoint=not(set(xs[0]['cpu_pool'])&set(xs[1]['cpu_pool'])),quota_decreases_before_completion=any(m==1 and cores for m,cores,_ in updates),quota_released_after_model_execution=any(m==0 and not cores for m,cores,_ in updates),all_outputs_accepted=all(x['all_accepted'] for x in xs))
 out=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/receipts/parallel-scheduler-selfcheck.json');out.write_text(json.dumps(dict(checks=checks,passed=all(checks.values()),scope='real sleeping subprocesses; tests scheduler semantics, not model performance'),indent=2))
 print(checks);assert all(checks.values())
