#!/usr/bin/env python3
"""Hand off at the completed latency/ablation boundary to the validated batch-only driver."""
import json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'out/paper-evidence-20261116';EXP=OUT/'export'
last=EXP/'attempts/ablation-r3-both/result.json'
while not last.exists():time.sleep(1)
subprocess.run(['systemctl','--user','stop','twins-evidence-campaign'],check=True)
partials=[p for p in (EXP/'attempts').iterdir() if (p/'started.json').exists() and not (p/'result.json').exists()]
for line in subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines():
 if not line.startswith('twins-evidence-'):continue
 info=json.loads(subprocess.check_output(['docker','inspect',line],text=True))[0]
 own=any(Path(m.get('Source','/')).is_relative_to(OUT/'remote-raw') for m in info.get('Mounts',[]))
 if own:subprocess.run(['docker','stop','-t','3',line],check=True)
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
 resources=p.parent/'resources.jsonl'
 if resources.exists():
  samples=[json.loads(l) for l in resources.read_text().splitlines() if l.strip()]
  v['original_interference_classification']=v.get('interference')
  v['interference']=c.audit_interference(samples,v['engine'],int(v['affinity'].split('-')[0]))
  c.save(p,v)
# Re-evaluate variation/interference supplements after the terminal-pass classifier correction.
subprocess.run(['/usr/bin/python3','experiments/paper_evidence/campaign.py','latency'],cwd=ROOT,check=True)
(EXP/'latency-finished.json').write_text(json.dumps(dict(utc_ns=time.time_ns(),last_result=str(last))))
subprocess.run(['/usr/bin/python3','experiments/paper_evidence/supplement_provenance.py'],cwd=ROOT,check=True)
# Candidate validation is separate; its failure must not erase or relabel A1 evidence.
subprocess.run(['/usr/bin/python3','experiments/paper_evidence/cuda_gates.py'],cwd=ROOT,check=False)
subprocess.run(['systemd-run','--user','--unit=twins-evidence-batches','--property=MemoryMax=80G','--working-directory='+str(ROOT),'/usr/bin/python3','experiments/paper_evidence/batches.py'],check=True)
