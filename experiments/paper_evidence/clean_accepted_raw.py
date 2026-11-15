#!/usr/bin/env python3
"""Reclaim only this campaign's validated, regenerable raw data. Export evidence is retained."""
import json,shutil,time,subprocess
from archive_index import update
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]/'out/paper-evidence-20261116';EXP=ROOT/'export';RAW=ROOT/'remote-raw'
while True:
 for p in (EXP/'attempts').glob('*/result.json'):
  try:v=json.loads(p.read_text())
  except (OSError,ValueError):continue
  raw=RAW/p.parent.name
  # Allow the producer to finish its own post-result cleanup before reclaiming its directory.
  if time.time_ns()-p.stat().st_mtime_ns<30_000_000_000:continue
  if v.get('accepted') and raw.is_dir() and raw.parent.resolve()==RAW.resolve():
   shutil.rmtree(raw)
   with (EXP/'raw-cleanup.jsonl').open('a') as f:f.write(json.dumps(dict(label=p.parent.name,utc_ns=time.time_ns(),reason='validated; timing, hashes, counts, logs and resources retained in export'))+'\n')
 update(ROOT)
 services=subprocess.run(['systemctl','--user','show','twins-evidence-campaign','twins-evidence-after-latency','twins-evidence-batches','-p','Id','-p','ActiveState','-p','SubState','-p','Result'],capture_output=True,text=True)
 health=dict(utc_ns=time.time_ns(),free_bytes=shutil.disk_usage(ROOT).free,services=services.stdout)
 tmp=EXP/'health.json.tmp';tmp.write_text(json.dumps(health,indent=2));tmp.replace(EXP/'health.json')
 if (EXP/'all-finished.json').exists():break
 time.sleep(30)
