#!/usr/bin/env python3
"""Hand off at the completed latency/ablation boundary to the validated batch-only driver."""
import json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'out/paper-evidence-20261116';EXP=OUT/'export'
last=EXP/'attempts/ablation-r3-both/result.json'
while not last.exists():time.sleep(1)
partials=[p for p in (EXP/'attempts').iterdir() if (p/'started.json').exists() and not (p/'result.json').exists()]
subprocess.run(['systemctl','--user','stop','twins-evidence-campaign'],check=True)
for line in subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines():
 if line.startswith('twins-evidence-'):subprocess.run(['docker','stop','-t','3',line])
for p in partials:
 if not p.exists():continue
 (p/'preempted.json').write_text(json.dumps(dict(reason='handoff after latency to driver excluding validation from batch makespan',utc_ns=time.time_ns())))
 dest=EXP/'preempted'/(p.name+'-batch-handoff');dest.parent.mkdir(exist_ok=True);p.rename(dest)
# Reclassify terminal flags, retaining diagnostics from every internal retry/pass.
import campaign as c
for p in (EXP/'attempts').glob('*/result.json'):
 v=json.loads(p.read_text());log=p.parent/'process.log'
 if v['engine']=='simai':log=p.parent/'err.txt'
 if not log.exists():continue
 f=c.flags(log.read_text(errors='replace'));old=v.get('flags')
 if old!=f:
  v['original_flag_classification']=old;v['flags']=f
  v['accepted']=v['rc']==0 and v['validation'].get('equal') and v.get('dag_validation',{'equal':True})['equal'] and not f['present']
  v['status']='accepted' if v['accepted'] else v.get('status','failed_or_flagged')
  c.save(p,v)
# Re-evaluate variation/interference supplements after the terminal-pass classifier correction.
subprocess.run(['/usr/bin/python3','experiments/paper_evidence/campaign.py','latency'],cwd=ROOT,check=True)
(EXP/'latency-finished.json').write_text(json.dumps(dict(utc_ns=time.time_ns(),last_result=str(last))))
subprocess.run(['systemd-run','--user','--unit=twins-evidence-batches','--property=MemoryMax=80G','--working-directory='+str(ROOT),'/usr/bin/python3','experiments/paper_evidence/batches.py'],check=True)
