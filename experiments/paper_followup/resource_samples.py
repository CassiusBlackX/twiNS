"""Small resource evidence for the shared measurement epoch."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

output=Path(sys.argv[1])
while True:
    memory={}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key,value=line.split(':',1)
        if key in ('MemAvailable','MemFree','SwapFree'):
            memory[key]=value.strip()
    processes=[]
    for p in Path('/proc').iterdir():
        try:
            if not p.name.isdigit() or p.stat().st_uid!=os.getuid():
                continue
            status=(p/'status').read_text()
            fields=dict(line.split(':',1) for line in status.splitlines() if ':' in line)
            if not fields['Name'].strip().startswith(('rdma-ref','twins')):
                continue
            processes.append(dict(pid=int(p.name),comm=fields['Name'].strip(),
                rss=fields.get('VmRSS','').strip(),peak_rss=fields.get('VmHWM','').strip(),
                affinity=sorted(os.sched_getaffinity(int(p.name)))))
        except (OSError,ProcessLookupError):
            pass
    try:
        gpu=subprocess.check_output(['nvidia-smi','--query-gpu=utilization.gpu,memory.used,power.draw',
                                     '--format=csv,noheader,nounits'],text=True,timeout=4).strip()
    except subprocess.SubprocessError as error:
        gpu=str(error)
    print(json.dumps(dict(utc_ns=time.time_ns(),memory=memory,load=os.getloadavg(),gpu=gpu,
                          own_model_processes=processes)),flush=True)
    if (output/'summary.json').exists():
        break
    time.sleep(30)
