#!/usr/bin/env python3
from pathlib import Path
p=Path(__file__).with_name('unified_runs.py');s=p.read_text()
s=s.replace("local/unified'","local/timer-regression'")
s=s.replace("row['kind'] in ('tcp','rdma') and row['reference_sha256']", "row['kind']=='tcp' and row['suite'] in ('tcp-main','tcp-grid') and row['reference_sha256']")
s=s.replace("passes=[(64,SRC/'build-final-clean/twins'),(128,SRC/'build-final-clean/twins128')]", "passes=[(128,SRC/'build-timer/twins128')]")
exec(compile(s,str(p),'exec'),{'__file__':str(p),'__name__':'__main__'})
