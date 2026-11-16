#!/usr/bin/env python3
"""Real subprocess overlap and P16 barrier check, without any model/GPU execution."""
import concurrent.futures,json,subprocess,sys,tempfile,threading,time
from pathlib import Path
import parallel_latency as p
with tempfile.TemporaryDirectory(dir='/tmp',prefix='twins-latency-scheduler-') as tmp:
 root=Path(tmp);p.c.EXPORT=root;p.P=root/'parallel';p.P.mkdir();p.available_gib=lambda:100
 mutex=threading.Lock();active={};seen=dict(cpu_overlap=False,p0_overlap=False,p16_exclusive=True,disjoint_cpu=True)
 def fake(label,j,e,opts=(),cpu=0,cap=None,repeat=False,resource_context=None):
  with mutex:
   if e=='p16':seen['p16_exclusive'] &= not active
   if e=='simai':
    seen['cpu_overlap'] |= any(x[0]=='simai' for x in active.values());seen['disjoint_cpu'] &= all(x[1]!=cpu for x in active.values() if x[0]=='simai')
   seen['p0_overlap'] |= e=='p0' and any(x[0]=='simai' for x in active.values()) or e=='simai' and any(x[0]=='p0' for x in active.values());active[label]=(e,cpu)
  start=time.perf_counter_ns();subprocess.run([sys.executable,'-c','import time;time.sleep(.06)'],check=True);v=dict(accepted=True,wall_ns=time.perf_counter_ns()-start)
  d=root/'attempts'/label;d.mkdir(parents=True,exist_ok=True);(d/'result.json').write_text(json.dumps(v))
  with mutex:active.pop(label)
  return v
 p.c.attempt=fake;j=dict(case='test',old_simai_s=2);tasks=[p.task('cpu1',j,'simai'),p.task('cpu2',j,'simai'),p.task('gpu0',j,'p0'),p.task('gpu16',j,'p16')];p.phase(tasks,dict(test=dict(cap_gib=1)))
 out=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/receipts/parallel-latency-selfcheck.json');out.write_text(json.dumps(dict(checks=seen,passed=all(seen.values()),scope='real subprocess control-flow test only; no simulation/performance claims'),indent=2));print(seen);assert all(seen.values())
 from unittest.mock import patch
 from types import SimpleNamespace
 p.c.RAW=root/'raw';p.c.RAW.mkdir()
 for name in ('old-next','new-managed'):
  d=root/'attempts'/name;d.mkdir();(d/'started.json').write_text('{}');(p.c.RAW/name).mkdir()
 old=root/'attempts/old';old.mkdir();(old/'result.json').write_text('{"accepted":true}')
 def control(cmd,**kwargs):return SimpleNamespace(returncode=1 if cmd[:2]==['docker','inspect'] else 0,stdout='')
 with patch.object(p.sp,'run',control):p.stop_old('old',{'new-managed'})
 handoff=dict(managed_attempt_preserved=(root/'attempts/new-managed/started.json').exists(),old_partial_archived=bool(list((root/'preempted').glob('old-next-*'))),completed_result_preserved=(old/'result.json').exists(),managed_raw_preserved=(p.c.RAW/'new-managed').exists())
 print('handoff',handoff);assert all(handoff.values())

