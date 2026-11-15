#!/usr/bin/env python3
import gzip,hashlib,json,subprocess,time
from pathlib import Path
SRC=Path(__file__).resolve().parents[2];OUT=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/local/corpus')
for d in sorted(OUT.iterdir()):
 if not d.is_dir() or not (d/'output.corpus.gz').exists():continue
 raw=d/'replay.corpus'
 with gzip.open(d/'output.corpus.gz','rb') as f,raw.open('wb') as g:
  while b:=f.read(1<<20):g.write(b)
 bin=SRC/'build-micro/twins-lineage-bench'
 with (d/'micro-v2.jsonl').open('w') as f:
  try:rc=subprocess.run([str(bin),str(raw)],stdout=f,stderr=subprocess.STDOUT,timeout=300).returncode
  except subprocess.TimeoutExpired:rc=124
 raw.unlink();records=[]
 for l in (d/'micro-v2.jsonl').read_text().splitlines():
  try:records.append(json.loads(l))
  except ValueError:pass
 v=dict(rc=rc,binary_sha256=hashlib.sha256(bin.read_bytes()).hexdigest(),accepted=rc==0 and len(records)>10 and all(x.get('mismatches',0)==0 for x in records),records=records,fp_scope='post-root suffix; complete walk input includes root-start prefix; two stored FP values uniquely determine checked suffix',old_invalid_micro_preserved='micro.jsonl')
 (d/'micro-v2-result.json').write_text(json.dumps(v,indent=2));print(d.name,'accepted',v['accepted'],flush=True)
