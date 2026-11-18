#!/usr/bin/env python3
"""Selective durable local backhaul and live Markdown. No raw traces/caches copied."""
import argparse,datetime,hashlib,json,subprocess,time
from pathlib import Path
from report import report,read
a=argparse.ArgumentParser();a.add_argument('--once',action='store_true');x=a.parse_args();E=Path('/home/cassius/Work/SpecDES/twiNS/out/isolated-evidence-20261118');D=E/'s50';(E/'receipts').mkdir(exist_ok=True);M=Path('/home/cassius/Projects/TwiNS');REMOTE='s50:Videos/twins-isolated-20261118/out/';cache={}
while True:
 for part in ('export','receipts'):
  dest=D/part;dest.mkdir(parents=True,exist_ok=True);r=subprocess.run(['rsync','-a','--exclude=*.tmp','--timeout=30','--bwlimit=4096','-e','ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10',REMOTE+part+'/',str(dest)+'/'],capture_output=True,text=True)
  with (E/'receipts/sync.jsonl').open('a') as f:f.write(json.dumps(dict(utc_ns=time.time_ns(),part=part,rc=r.returncode,stderr=r.stderr[:2000]))+'\n')
 idx=read(D/'export/archive-index.json');bad=[];missing=[];verified=0
 for rel,v in idx.get('files',{}).items():
  p=D/rel
  if not p.exists():missing.append(rel);continue
  s=p.stat();key=(rel,s.st_size,s.st_mtime_ns,v['sha256'])
  if key not in cache:
   h=hashlib.sha256()
   with p.open('rb') as f:
    while b:=f.read(8<<20):h.update(b)
   if h.hexdigest()!=v['sha256']:bad.append(rel);continue
   cache[key]=True
  verified+=1
 check=dict(utc_ns=time.time_ns(),remote_index_utc_ns=idx.get('utc_ns'),verified=verified,indexed=len(idx.get('files',{})),missing=missing,hash_mismatches=bad,passed=bool(idx) and not bad and not missing,scope=idx.get('scope'));(E/'receipts/archive-verification.json').write_text(json.dumps(check,indent=2)+'\n');report(E,M)
 done=read(D/'export/all-finished.json')
 if x.once or done and check['passed'] and idx.get('utc_ns',0)>=done['utc_ns']:break
 time.sleep(30)
