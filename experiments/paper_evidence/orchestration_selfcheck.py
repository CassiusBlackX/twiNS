#!/usr/bin/env python3
"""Checks only the resource classifier; does not claim experimental interference was absent."""
import json
from pathlib import Path
from campaign import audit_interference

def sample(foreign):
 return dict(pid=1,own_processes=[dict(pid=1,uticks=0,sticks=0)],gpu='30, 100, 100, 1000, 50',gpu_processes=('2, other, 100' if foreign else '1, twins-ai, 100'),cpu_stat='')
a=audit_interference([sample(x) for x in (1,0,1,0,1)],'p0');b=audit_interference([sample(1)]*3,'p0');own=audit_interference([sample(0)]*4,'p16')
checks=dict(nonconsecutive_does_not_trigger=not a['suspected'],three_consecutive_triggers=b['suspected'],own_gpu_not_foreign=not own['suspected'],p16_affinity_is_17_logical=own['bound_cpus']==list(range(17)))
p=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/receipts/orchestration-selfcheck.json');p.write_text(json.dumps(dict(passed=all(checks.values()),checks=checks,scope='synthetic classifier selfcheck, not scientific simulation evidence'),indent=2)+'\n');print(checks);assert all(checks.values())
