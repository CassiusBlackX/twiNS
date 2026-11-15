#!/usr/bin/env python3
"""New audit receipts. Does not invent wall times for historical campaigns."""
import csv,hashlib,json,re,subprocess,time
from pathlib import Path
SRC=Path(__file__).resolve().parents[2];DATA=Path('/home/cassius/Work/SpecDES/twiNS');OUT=DATA/'out/paper-evidence-20261116/local/unified';OUT.mkdir(parents=True,exist_ok=True)
rows=json.loads((DATA/'out/paper-evidence-20261116/manifests/scenario-inventory.json').read_text());seen={}
for row in rows:
 if row['kind'] in ('tcp','rdma') and row['reference_sha256']:
  key=row['execution_config_sha256'];seen.setdefault(key,dict(row,memberships=[]))['memberships'].append([row['suite'],row['case']])
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
for key,row in seen.items():
 d=OUT/key;d.mkdir(exist_ok=True)
 if (d/'result.json').exists():continue
 if row['kind']=='tcp':passes=[(64,SRC/'build-final-clean/twins'),(128,SRC/'build-final-clean/twins128')]
 else:passes=[(None,SRC/'build-final-clean/twins-rdma')]
 attempts=[]
 for cap,bin in passes:
  logpath=d/f'{cap}.log';output=d/'output';cmd=[str(bin)]
  if row['kind']=='rdma':cmd+=[str(SRC/'validation/hpcc/hpcc.cfg')]
  cmd+=row['args']+['--out='+str(output)]
  start=time.perf_counter_ns();utc=time.time_ns()
  with logpath.open('w') as f:
   try:rc=subprocess.run(cmd,cwd=DATA,stdout=f,stderr=subprocess.STDOUT,timeout=3600).returncode
   except subprocess.TimeoutExpired:rc=124
  wall=time.perf_counter_ns()-start;log=logpath.read_text(errors='replace');v=dict(capacity_bytes=cap,command=cmd,binary_sha256=sha(bin),rc=rc,start_utc_ns=utc,wall_ns=wall,timeout=rc==124,flagged=bool(re.search(r'were undecided|warning:.*(?:ties the chains|not exact)|error:',log)),overflow=bool(re.search('capacity overflow|a capacity overflowed',log)),diagnostics=[x for x in log.splitlines() if re.search('warning:|error:|undecided.ties|lost tails|overflows:|running again',x)])
  try:
   if row['kind']=='tcp':
    def read(p):
     with open(p) as f:r=csv.reader(f);head=next(r);return [tuple(x[:6]) for x in r]
    a=read(output);b=read(row['reference'])
   else:
    def read(p):return sorted(tuple(x.split()) for x in Path(p).read_text().splitlines())
    a=read(output);b=read(row['reference'])
   v['validation']=dict(equal=a==b,actual_rows=len(a),reference_rows=len(b),actual_sha256=sha(output),reference_sha256=row['reference_sha256'],fields='first 6 CSV columns' if row['kind']=='tcp' else 'all UID/start/FCT columns')
  except Exception as e:v['validation']=dict(equal=False,error=str(e))
  attempts.append(v)
  if row['kind']!='tcp' or rc!=0 or not v['flagged']:break
 result=dict(config=row,attempts=attempts,cumulative_wall_ns=sum(x['wall_ns'] for x in attempts),final_status='timeout' if v['timeout'] else 'refused_or_error' if rc not in (0,2) else 'stranded' if rc==2 else 'flagged' if v['flagged'] or v['overflow'] else 'complete_unflagged' if v['validation'].get('equal') else 'mismatch')
 (d/'result.json').write_text(json.dumps(result,indent=2)+'\n')
 with (OUT/'attempts.jsonl').open('a') as f:f.write(json.dumps(result)+'\n')
 if output.exists() and v['validation'].get('equal'):output.unlink()
 print(row['suite'],row['case'],result['final_status'],flush=True)
(OUT/'finished.json').write_text(json.dumps(dict(utc_ns=time.time_ns(),configs=len(seen))))
