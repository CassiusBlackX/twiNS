#!/usr/bin/env python3
"""Resolve legacy direct-SimAI .fct/.sent evidence, without rerunning models or rewriting inventory IDs."""
import csv,hashlib,json,re,shlex
from pathlib import Path
R=Path('/home/cassius/Work/SpecDES/twiNS');E=R/'out/paper-evidence-20261116';H=R/'out/reconcile_20261116'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
def readrows(p):return [tuple(map(int,l.split())) for l in p.read_text().splitlines() if l.strip() and not l.startswith('#')]
def check(a,b):
 if not a.exists() or not b.exists():return dict(available=False)
 x,y=readrows(a),readrows(b);return dict(available=True,equal=sorted(x)==sorted(y),actual_rows=len(x),reference_rows=len(y),actual_duplicate_ids=len(x)-len({q[0] for q in x}),reference_duplicate_ids=len(y)-len({q[0] for q in y}),actual_sha256=sha(a),reference_sha256=sha(b))
rows=[]
def resolve_job(x):
 suite=Path(__file__).resolve().parents[2]/'validation/simai'/(x['suite'].removeprefix('ai-')+'.txt')
 fields=next(shlex.split(l) for l in suite.read_text().splitlines() if l.split()[:1]==[x['case']])
 paths=[]
 for name in fields[1:3]:
  paths.append(next(R/'data'/d/name for d in ('simai','rdma','topo','flows') if (R/'data'/d/name).is_file()))
 return dict(job_file=str(suite),job_file_sha256=sha(suite),job_fields=fields,input_sha256={str(p):sha(p) for p in paths},config=str(suite.parent/'SimAI.conf'),config_sha256=sha(suite.parent/'SimAI.conf'),scope='resolved definition and present-day hashes, not proof of the unknown historical binary or historical input identity')
for x in json.loads((E/'manifests/scenario-inventory.json').read_text()):
 if not x['kind'].startswith('ai') or x['reference_sha256'] is not None:continue
 n=x['case'];ref=R/f'out/simai/ref/{n}.fct';sent=ref.with_suffix('.sent');actual=R/f'out/simai/sim/{n}.fct';log=actual.with_suffix('.log');text=log.read_text(errors='replace') if log.exists() else '';m=re.findall(r'flows done (\d+) of (\d+)',text)
 rows.append(dict(suite=x['suite'],case=n,original_execution_key=x['execution_config_sha256'],reference_fct=str(ref),reference_sent=str(sent),reference_fct_sha256=sha(ref),reference_sent_sha256=sha(sent),fct=check(actual,ref),sent=check(actual.with_suffix('.sent'),sent),flow_completion=dict(done=int(m[-1][0]),total=int(m[-1][1])) if m else None,legacy_log_sha256=sha(log),legacy_log=str(log),source_identity='not recovered from these per-case files; do not relabel as the frozen48e0/6bc candidate',historical_internal_pass_cost='unknown',scope='read-only revalidation of old local outputs, not new model execution or proof of the current source'))
for row in rows:
 original=next(x for x in json.loads((E/'manifests/scenario-inventory.json').read_text()) if x['execution_config_sha256']==row['original_execution_key'])
 row['resolved_definition']=resolve_job(original)
legacy_paper=list(csv.DictReader((H/'ai_current.csv').open()));new=[]
for p in (E/'s50/export/attempts').glob('*/result.json'):
 x=json.loads(p.read_text())
 if x['label'].startswith(('latency-','ablation-','candidate-')):new.append(dict(case=x['case'],engine=x['engine'],label=x['label'],accepted=x['accepted'],binary_sha256=x['binary_sha256'],receipt=str(p.relative_to(E))))
v=dict(legacy_direct=rows,legacy_paper_42=legacy_paper,current_ai_receipts=new,original_missing_reference_entries=len(rows),resolved_with_fct_and_sent=sum(bool(x['reference_fct_sha256'] and x['reference_sent_sha256']) for x in rows),legacy_outputs_matching=sum(x['fct'].get('equal',False) and x['sent'].get('equal',False) for x in rows),scope='sidecar only; initial inventory keys and historical timings are preserved; missing binary provenance or internal-pass wall is explicitly unknown')
(E/'reports/ai-evidence-index.json').write_text(json.dumps(v,indent=2)+'\n');print({k:v[k] for k in ('original_missing_reference_entries','resolved_with_fct_and_sent','legacy_outputs_matching')})
