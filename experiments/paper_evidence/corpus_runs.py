#!/usr/bin/env python3
import gzip,hashlib,json,os,re,subprocess,time
from pathlib import Path
SRC=Path(__file__).resolve().parents[2];DATA=Path('/home/cassius/Work/SpecDES/twiNS');ROOT=DATA/'out/paper-evidence-20261116';OUT=ROOT/'local/corpus';OUT.mkdir(parents=True,exist_ok=True)
for name,g in [('rdma-lockstep',0),('c_allgather_512m_hpn1024',1024),('gpt3_13b_probe_hpn128',128)]:
 d=OUT/name;d.mkdir(exist_ok=True)
 if (d/'result.json').exists():continue
 if g:
  cmd=[str(SRC/'build-corpus/twins-ai'),str(SRC/'validation/simai/SimAI.conf'),'--topo='+str(DATA/f'data/simai/hpn{g}.topo'),'--astra='+str(DATA/f'out/simai/wl/paper/{name.replace("_hpn","_")}.txt'),'--dag-out='+str(d/'dag')];ref=DATA/f'out/simai/paper/{name}.ref'
 else:
  cmd=[str(SRC/'build-corpus/twins-rdma'),str(SRC/'validation/hpcc/hpcc.cfg'),'--topo='+str(DATA/'data/topo/fattree8.txt'),'--flows='+str(DATA/'data/rdma/k8_perm.csv'),'CC_MODE=3','BUFFER_SIZE=1'];ref=DATA/'out/rdma/ref/k8_perm_b1_cc3.fct'
 cmd+=['--out='+str(d/'output'),'--fp-bases=1000003,1000033']
 with (d/'process.log').open('w') as f:
  start=time.perf_counter_ns()
  try:rc=subprocess.run(cmd,cwd=d,stdout=f,stderr=subprocess.STDOUT,timeout=3600).returncode
  except subprocess.TimeoutExpired:rc=124
  wall=time.perf_counter_ns()-start
 def read(p):return sorted(tuple(map(int,l.split())) for l in p.read_text().splitlines()) if p.exists() else None
 equal=read(d/'output')==read(ref);dag_equal=True
 if g:
  def norm(p):return sorted(re.sub(r'^(on [ft][0-9]* m).*',r'\1',l) for l in p.read_text().splitlines() if not l.startswith('#'))
  dag_equal=norm(d/'dag')==norm(DATA/f'out/simai/paper/{name}.dag')
 log=(d/'process.log').read_text(errors='replace');corpus=d/'output.corpus';v=dict(case=name,command=cmd,rc=rc,wall_ns=wall,output_equal=equal,dag_equal=dag_equal,flagged=bool(re.search(r'warning:.*(?:not exact|ties the chains)|error:',log)),corpus_sha256=hashlib.sha256(corpus.read_bytes()).hexdigest() if corpus.exists() else None)
 if corpus.exists() and rc==0 and equal and dag_equal and not v['flagged']:
  with (d/'micro.jsonl').open('w') as f:
   try:mrc=subprocess.run([str(SRC/'build-micro/twins-lineage-bench'),str(corpus)],stdout=f,stderr=subprocess.STDOUT,timeout=300).returncode
   except subprocess.TimeoutExpired:mrc=124
  v['micro_rc']=mrc
  records=[]
  for l in (d/'micro.jsonl').read_text().splitlines():
   try:records.append(json.loads(l))
   except ValueError:pass
  v['micro_checks_equal']=mrc==0 and bool(records) and all(x.get('mismatches',0)==0 for x in records)
  v['micro_records']=records
  with corpus.open('rb') as f,gzip.open(str(corpus)+'.gz','wb',compresslevel=6) as z:
   while b:=f.read(1<<20):z.write(b)
  corpus.unlink()
 (d/'result.json').write_text(json.dumps(v,indent=2)+'\n');print(json.dumps(v),flush=True)
(OUT/'finished.json').write_text(json.dumps(dict(utc_ns=time.time_ns())))
