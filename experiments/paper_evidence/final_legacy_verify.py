#!/usr/bin/env python3
"""Recheck only legacy direct-SimAI mismatches using the already-built candidate.

This is a correctness check, not a latency sample or a replacement of old receipts.
"""
import hashlib,json,re,shlex,subprocess,time
from pathlib import Path
R=Path('/home/cassius/Work/SpecDES/twiNS'); S=Path(__file__).resolve().parents[2]
E=R/'out/paper-evidence-20261116'; B=S/'build-boundary-candidate/twins-rdma'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def rows(p): return [tuple(map(int,l.split())) for l in p.read_text().splitlines() if l.strip() and not l.startswith('#')]
def find(n):
 for d in ('simai','rdma','topo','flows'):
  p=R/'data'/d/n
  if p.is_file(): return p
 raise FileNotFoundError(n)
def main():
 index=json.loads((E/'reports/ai-evidence-index.json').read_text()); results=[]
 for old in index['legacy_direct']:
  if old['fct'].get('equal') and old['sent'].get('equal'): continue
  name=old['case']; line=next(shlex.split(l) for l in (S/'validation/simai/jobs_dctcp.txt').read_text().splitlines() if l.split()[:1]==[name])
  _,topo,flow,*opts=line; d=E/'local/final-ai-direct'/name; d.mkdir(parents=True,exist_ok=True)
  cmd=[str(B),str(S/'validation/simai/SimAI.conf'),'--topo='+str(find(topo)),'--flows='+str(find(flow)),*opts,'--out='+str(d/'output.fct'),'--sent='+str(d/'output.sent'),'--fp-bases=1000003,1000033']
  start=time.time_ns()
  with (d/'process.log').open('w') as f: rc=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,timeout=120).returncode
  wall=time.time_ns()-start; text=(d/'process.log').read_text(); terminal=re.split(r'^.*running again with[^\n]*\n',text,flags=re.M)[-1]
  bad=bool(re.search(r'warning:|error:|undecided ties [1-9]|on lost tails [1-9]|(?:tx|queue|ackq|qps|due) [1-9]',terminal))
  equal={ext:sorted(rows(d/('output.'+ext)))==sorted(rows(Path(old['reference_'+ext]))) for ext in ('fct','sent')}
  complete=re.findall(r'flows done (\d+) of (\d+)',terminal)
  v=dict(case=name,command=cmd,rc=rc,wall_ns=wall,binary_sha256=sha(B),source_core='6bc5a7cbdafc5fed232474ea85d74c81b00aa126',config_sha256=sha(S/'validation/simai/SimAI.conf'),input_sha256={str(p):sha(p) for p in (find(topo),find(flow))},reference_sha256={ext:sha(Path(old['reference_'+ext])) for ext in ('fct','sent')},output_equal=equal,flagged=bad,completion=complete[-1] if complete else None,accepted=rc==0 and all(equal.values()) and not bad,scope='new candidate compatibility check; old mismatching outputs and their unknown provenance remain unchanged')
  (d/'result.json').write_text(json.dumps(v,indent=2)+'\n');results.append(v)
 out=dict(checks=results,count=len(results),accepted=sum(x['accepted'] for x in results),scope='only seven newly discovered legacy output mismatches; no new reference generation or performance measurement')
 (E/'reports/ai-legacy-recheck.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(dict(count=out['count'],accepted=out['accepted'])))
if __name__=='__main__': main()
