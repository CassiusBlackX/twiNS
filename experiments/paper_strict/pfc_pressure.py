"""Choose bounded PFC stress settings by validating physical headroom first."""
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

def pressure(root,workspace):
    root.mkdir(parents=True)
    tree=workspace/'twins-paper-release-20261122'
    paper=workspace/'twiNS'
    source=paper/'out/simai/wl/paper/c_alltoall_64m_128.txt'
    topology=paper/'data/simai/dcn128.topo'
    binary=root.parent/'rx-counters/compiled/twins-ai'
    config=tree/'validation/simai/SimAI.conf'
    reserved=Counter()
    for row in topology.read_text().splitlines()[2:]:
        a,b,speed,delay,_=row.split()
        rate=Decimal(speed.removesuffix('Gbps'))*1_000_000_000
        latency=Decimal(delay.removesuffix('ms'))/1000
        for node in (int(a),int(b)):
            if node>=144:  # inter-server switches in the fixed DCN+ input
                reserved[node]+=int(rate*latency/8)*3+4096
    orders=((16,256),(8,256),(4,64))
    proposed=[]
    for mib,size in orders:
        headroom={node:mib*1024**2-value for node,value in reserved.items()}
        if min(headroom.values())<=0:
            proposed.append(dict(buffer_mib=mib,burst_mib=size,min_non_reserved_bytes=min(headroom.values()),allowed=False))
            continue
        d=root/f'b{mib}-{size}m'
        d.mkdir()
        input_file=d/'workload.log'
        input_file.write_text(source.read_text().replace('67108864',str(size*1024**2)))
        argv=[binary,config,f'--topo={topology}',f'--astra={input_file}',f'BUFFER_SIZE={mib}',
              '--out=messages.fct','--dag-out=actions.dag','--nvl-islands=0','--gwin=0','--fp-bases=1000003,1000033']
        start=time.monotonic_ns()
        with (d/'process.log').open('wb') as log:
            try:
                rc=subprocess.run(list(map(str,argv)),cwd=d,stdout=log,stderr=subprocess.STDOUT,timeout=1200).returncode
            except subprocess.TimeoutExpired:
                rc=124
            elapsed=time.monotonic_ns()-start
        cursor=Counter()
        for line in (d/'process.log').open():
            if line.startswith('pfc_switch '):
                _,sw,group,ports,num,ticks=line.split()
                group,ports,num,ticks=map(int,(group,ports,num,ticks))
                if ports:
                    cursor['busy']+=num
                    cursor['paused']+=num if group&2 else 0
                    cursor['parallel']+=num if group&1 else 0
                    cursor['paused_ticks']+=ticks if group&2 else 0
                    cursor['busy_ticks']+=ticks
        text=(d/'process.log').read_text()
        status=re.findall(r'drops (\d+) marks (\d+) pauses (\d+) resumes (\d+)',text)
        row=dict(buffer_mib=mib,burst_mib=size,min_non_reserved_bytes=min(headroom.values()),allowed=True,
                 rc=rc,wall_ns=elapsed,command=list(map(str,argv)),activity=dict(cursor),counters=status[-1:])
        (d/'result.json').write_text(json.dumps(row,indent=2)+'\n')
        proposed.append(row)
    (root/'summary.json').write_text(json.dumps(proposed,indent=2)+'\n')

if __name__=='__main__':
    pressure(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve())
