#!/usr/bin/env python3
"""Index completed selected evidence only; never index/copy raw traces or caches."""
import hashlib,json,os,subprocess,time
from pathlib import Path
R=Path(__file__).resolve().parent.parent;E=R/'out';X=E/'export';cache={}
def read(p):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {}
def digest(p):
 s=p.stat();key=(str(p),s.st_size,s.st_mtime_ns)
 if key not in cache:
  h=hashlib.sha256()
  with p.open('rb') as f:
   while b:=f.read(8<<20):h.update(b)
  cache[key]=h.hexdigest()
 return dict(bytes=s.st_size,sha256=cache[key])
def save(p,v):
 q=p.with_suffix('.tmp');q.write_text(json.dumps(v,indent=2)+'\n');q.replace(p)
while True:
 files=set()
 for root in ('cubic','tcp-scans','attempts','mtp','pilots'):
  for p in (X/root).glob('*/result.json'):
   files.update(q for q in p.parent.rglob('*') if q.is_file() and not q.name.endswith('.tmp'))
 for p in (X/'ai-batches').glob('*.json'):
  v=read(p)
  if 'makespan_ns' in v:
   files.add(p);files.update(q for q in (p.parent/v['trial']).rglob('*') if q.is_file() and not q.name.endswith('.tmp'))
 for p in X.glob('*.json'):
  if p.name not in ('archive-index.json','health.json','state.json'):files.add(p)
 for p in (X/'mtp').glob('summary-*.json'):files.add(p)
 if read(X/'pilot-pipeline-finished.json'):files.update(p for p in (X/'pilot-pipeline').rglob('*') if p.is_file() and not p.name.endswith('.tmp'))
 for root in ('interrupted-scans','preempted-cubic'):files.update(p for p in (X/root).rglob('*') if p.is_file() and not p.name.endswith('.tmp'))
 for p in (X/'ai-batches').glob('*trial*/state.json'):
  if read(p).get('status')=='interrupted':files.update(q for q in p.parent.rglob('*') if q.is_file() and not q.name.endswith('.tmp'))
 if read(E/'receipts/prepared.json'):files.update(p for p in (E/'receipts').rglob('*') if p.is_file() and not p.name.endswith('.tmp'))
 idx={}
 for p in sorted(files):
  try:idx[str(p.relative_to(E))]=digest(p)
  except FileNotFoundError:pass
 save(X/'archive-index.json',dict(utc_ns=time.time_ns(),files=idx,scope='completed exported receipts, compact summaries, build/source records; no raw traces, reference caches or validation caches'))
 free=os.statvfs(E);save(X/'health.json',dict(utc_ns=time.time_ns(),free_bytes=free.f_bavail*free.f_frsize,services=subprocess.run(['systemctl','--user','show','twins-isolated-campaign','--property=ActiveState,Result'],text=True,capture_output=True).stdout))
 if read(X/'all-finished.json'):break
 time.sleep(30)
