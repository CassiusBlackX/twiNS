#!/usr/bin/env python3
"""Reclaim only this campaign's validated, regenerable raw data. Export evidence is retained."""
import json,shutil,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]/'out/paper-evidence-20261116';EXP=ROOT/'export';RAW=ROOT/'remote-raw'
while True:
 for p in (EXP/'attempts').glob('*/result.json'):
  try:v=json.loads(p.read_text())
  except (OSError,ValueError):continue
  raw=RAW/p.parent.name
  if v.get('accepted') and raw.is_dir() and raw.parent.resolve()==RAW.resolve():
   shutil.rmtree(raw)
   with (EXP/'raw-cleanup.jsonl').open('a') as f:f.write(json.dumps(dict(label=p.parent.name,utc_ns=time.time_ns(),reason='validated; timing, hashes, counts, logs and resources retained in export'))+'\n')
 if (EXP/'all-finished.json').exists():break
 time.sleep(30)
