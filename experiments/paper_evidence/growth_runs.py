#!/usr/bin/env python3
import hashlib,json,os,re,signal,subprocess,time
from pathlib import Path
SRC=Path(__file__).resolve().parents[2];ROOT=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116');OUT=ROOT/'local/growth';OUT.mkdir(parents=True,exist_ok=True);REF=ROOT/'s50/export/growth-references'
strict=os.environ.get('TWINS_GROWTH_STRICT_ONLY')=='1'
for scale in (1,2,4,8):
 meta=json.loads((REF/f'x{scale}/result.json').read_text())
 if not meta['accepted']:raise RuntimeError('unaccepted new reference')
 for variant in (['strict'] if strict else ['clean','profile']):
  d=OUT/f'x{scale}-{variant}';d.mkdir(exist_ok=True)
  if (d/'result.json').exists():continue
  bin=SRC/f'build-final-{variant}/twins-rdma';cmd=[str(bin),str(SRC/'validation/hpcc/hpcc.cfg'),'--topo=/home/cassius/Work/SpecDES/twiNS/data/topo/fattree8.txt','--flows='+str(REF/f'x{scale}/flows.csv'),'--out='+str(d/'output'),'CC_MODE=3','BUFFER_SIZE=1','--fp-bases=1000003,1000033']
  start=time.perf_counter_ns()
  with (d/'process.log').open('w') as f:
   p=subprocess.Popen(cmd,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
   try:rc=p.wait(timeout=3600)
   except subprocess.TimeoutExpired:
    os.killpg(p.pid,signal.SIGTERM)
    try:p.wait(5)
    except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
    rc=124
  wall=time.perf_counter_ns()-start;log=(d/'process.log').read_text(errors='replace')
  def read(p):return sorted(tuple(map(int,l.split())) for l in p.read_text().splitlines()) if p.exists() else None
  actual=read(d/'output');ref=read(REF/f'x{scale}/reference.fct');v=dict(scale=scale,variant=variant,command=cmd,binary_sha256=hashlib.sha256(bin.read_bytes()).hexdigest(),wall_ns=wall,rc=rc,timeout=rc==124,equal=actual==ref and actual is not None,rows=len(actual) if actual is not None else None,reference_rows=len(ref),flagged=bool(re.search(r'warning:.*(?:not exact|ties the chains)|error:',log)),metrics=[l for l in log.splitlines() if l.startswith(('evidence_','prof: chain','lead fingerprints:','drops '))])
  (d/'result.json').write_text(json.dumps(v,indent=2));print(json.dumps({k:v[k] for k in ('scale','variant','wall_ns','rc','equal','flagged')}),flush=True)
(OUT/('strict-finished.json' if strict else 'finished.json')).write_text(json.dumps(dict(utc_ns=time.time_ns())))
