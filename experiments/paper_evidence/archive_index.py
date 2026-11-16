#!/usr/bin/env python3
"""Checksums for completed exported evidence, never for bulk remote-raw traces."""
import hashlib,json,subprocess,time
from pathlib import Path
def update(root):
 exp=root/'export';dest=exp/'local-archive-index.json';old=json.loads(dest.read_text()).get('files',{}) if dest.exists() else {};files={}
 parents={p.parent for p in exp.rglob('result.json')}
 for parent in parents:
  for p in parent.iterdir():
   if not p.is_file() or p.name.endswith('.tmp'):continue
   st=p.stat();key=str(p.relative_to(exp));prior=old.get(key,{})
   if prior.get('mtime_ns')==st.st_mtime_ns and prior.get('bytes')==st.st_size:files[key]=prior;continue
   h=hashlib.sha256()
   with p.open('rb') as f:
    while b:=f.read(1<<20):h.update(b)
   files[key]=dict(bytes=st.st_size,mtime_ns=st.st_mtime_ns,sha256=h.hexdigest())
 for p in (exp/'provenance-supplement').rglob('*'):
  if p.is_file():files[str(p.relative_to(exp))]=dict(bytes=p.stat().st_size,mtime_ns=p.stat().st_mtime_ns,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
 for p in (exp/'parallel').glob('*'):
  if p.is_file() and p.name not in ('active.json','batches-active.json','admission-wait.json') and not p.name.endswith('.tmp'):
   files[str(p.relative_to(exp))]=dict(bytes=p.stat().st_size,mtime_ns=p.stat().st_mtime_ns,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
 v=dict(utc_ns=time.time_ns(),scope='completed exported receipts and retrospective input provenance; no remote-raw traces',files=files);tmp=dest.with_suffix('.tmp');tmp.write_text(json.dumps(v,indent=2)+'\n');tmp.replace(dest)
