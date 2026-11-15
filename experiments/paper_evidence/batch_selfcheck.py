#!/usr/bin/env python3
"""Real subprocess scheduling check: validation cannot enter batch makespan; memory admission works.
Uses sleeping child processes, never produces scientific simulation data.
"""
import json,sys,tempfile,time
from pathlib import Path
import batches as b
with tempfile.TemporaryDirectory(prefix='twins-batch-selfcheck-',dir='/tmp') as tmp:
 root=Path(tmp);b.BROOT=root/'export';b.BROOT.mkdir();b.RAW=root/'raw';b.RAW.mkdir();b.c.JOBS=[dict(case='first',old_simai_s=2),dict(case='second',old_simai_s=1)]
 def prepare(trial,j,engine,cpu,policy):
  d=trial/j['case'];d.mkdir();raw=b.RAW/(trial.name+'-'+j['case']);raw.mkdir();return dict(j=j,d=d,raw=raw,container='',v=dict(case=j['case'],engine='p0',command=[sys.executable,'-c','import time;time.sleep(.20)','--cpuset-cpus','0']))
 def validate(task):time.sleep(.30);return dict(case=task['j']['case'],accepted=True)
 b.prepare=prepare;b.validate=validate;b.c.snapshot=lambda:dict(utc_ns=time.time_ns())
 policy=dict(jobs={x['case']:dict(cap_gib=1) for x in b.c.JOBS})
 b.batch('overlap','simai',2,policy);a=json.loads((b.BROOT/'overlap.json').read_text())
 policy=dict(jobs={x['case']:dict(cap_gib=40) for x in b.c.JOBS})
 b.batch('memory','simai',2,policy);m=json.loads((b.BROOT/'memory.json').read_text())
 checks=dict(overlap_peak_two=a['peak_actual_concurrency']==2,overlap_admitted_before_finish=[x['action'] for x in a['events'][:2]]==['admit','admit'],validation_outside_timer=a['validation_ns']>a['makespan_ns'],memory_budget_forces_one=m['peak_actual_concurrency']==1,reservation_within_70=all(e['reserved_gib']<=70 for e in m['events']))
 result=dict(checks=checks,passed=all(checks.values()),overlap=a,memory=m,scope='scheduler selfcheck, not a simulator benchmark')
 out=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/receipts/batch-selfcheck.json');out.write_text(json.dumps(result,indent=2))
 print(json.dumps(checks));assert result['passed']
