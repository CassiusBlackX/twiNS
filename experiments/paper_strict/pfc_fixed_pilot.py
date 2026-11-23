"""Build the corrected PFC diagnostic and seek an independently confirmable pause setting."""
from collections import Counter
from decimal import Decimal
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from gpu_counters import build

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def boundary_test(out,source):
    from gpu_counters import SWITCH_LAYOUT
    kernel=(source/'src/rdma/rstep.cuh').read_text()
    kmax=int(re.search(r'constexpr int KMAX = (\d+)',kernel)[1])
    (out/'strict_switch_layout.h').write_text(SWITCH_LAYOUT)
    code=r'''
#define __host__
#define __device__
#include "strict_switch_layout.h"
#include <cassert>
#include <cstdio>
#include <set>
int main() {
    using H = StrictSwitchLayout<MAX_DEGREE>;
    std::set<std::size_t> keys;
    for (int sw=0;sw<3;++sw) for (int group=0;group<H::groups;++group)
        for (int ports=0;ports<=MAX_DEGREE;++ports) {
            auto count=H::index(sw,group,ports);
            assert(count+1<3*H::stride);
            assert(keys.insert(count).second && keys.insert(count+1).second);
        }
    assert(keys.size()==3*H::stride);
    printf("max_degree=%d distinct_counter_locations=%zu\n",MAX_DEGREE,keys.size());
}
'''
    fixture=out/'histogram-fixture.cc'
    fixture.write_text(code)
    with (out/'histogram-fixture.log').open('wb') as log:
        subprocess.run(['c++','-std=c++17',f'-DMAX_DEGREE={kmax}',fixture,'-o',out/'histogram-fixture'],
                       stdout=log,stderr=subprocess.STDOUT,check=True)
        subprocess.run([out/'histogram-fixture'],stdout=log,stderr=subprocess.STDOUT,check=True)

def check_pressure(output,workspace,compiled):
    source=workspace/'twins-paper-release-20261122'
    paper=workspace/'twiNS'
    model=paper/'out/simai/wl/paper/c_alltoall_64m_128.txt'
    topo=paper/'data/simai/dcn128.topo'
    cfg=source/'validation/simai/SimAI.conf'
    reserved=Counter()
    for row in topo.read_text().splitlines()[2:]:
        a,b,speed,delay,_=row.split()
        rate=Decimal(speed.removesuffix('Gbps'))*1_000_000_000
        latency=Decimal(delay.removesuffix('ms'))/1000
        for node in (int(a),int(b)):
            if node>=144:
                reserved[node]+=int(rate*latency/8)*3+4096
    # Retain the known 32/64 anchor. Smaller bursts avoid reusing the previously
    # timed-out 256 MiB controls at reduced pool sizes.
    design=((32,64),(24,32),(20,16),(16,8),(12,4),(8,4),(6,2),(6,4),(6,8),(8,8),(12,8))
    rows=[]
    for pool,burst in design:
        out=output/f'b{pool}-{burst}m'
        out.mkdir(parents=True)
        minimum=pool*1024**2-max(reserved.values())
        if minimum<=0:
            rows.append(dict(pool_mib=pool,burst_mib=burst,minimum_unreserved_bytes=minimum,rejected='invalid headroom'))
            continue
        workload=out/'workload.log'
        workload.write_text(model.read_text().replace('67108864',str(burst*1024**2)))
        args=[str(compiled/'twins-ai'),str(cfg),f'--topo={topo}',f'--astra={workload}',f'BUFFER_SIZE={pool}',
              '--nvl-islands=0','--gwin=0','--fp-bases=1000003,1000033','--out=messages.fct','--dag-out=actions.dag']
        start=time.monotonic_ns()
        with (out/'process.log').open('wb') as log:
            try:
                rc=subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,timeout=150).returncode
            except subprocess.TimeoutExpired:
                rc=124
            elapsed=time.monotonic_ns()-start
        by_switch=defaultdict_data(out/'process.log')
        eligible=[(s,c) for s,c in by_switch.items() if c['busy']>=1000]
        hotspot=max(eligible,key=lambda item:item[1]['paused']/item[1]['busy'],default=None)
        messages=out/'messages.fct'
        action=out/'actions.dag'
        receipt=dict(pool_mib=pool,burst_mib=burst,minimum_unreserved_bytes=minimum,
            rc=rc,process_wall_ns=elapsed,command=args,hotspot=hotspot,by_switch=by_switch,
            messages=sum(1 for _ in messages.open()) if messages.is_file() else None,
            output_hashes={p.name:digest(p) for p in (messages,action) if p.is_file()},
            input_hashes={str(p):digest(p) for p in (workload,topo,cfg)})
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        rows.append({k:v for k,v in receipt.items() if k!='by_switch'})
        (output/'pilot-summary.json').write_text(json.dumps(rows,indent=2)+'\n')
        print(pool,burst,rc,hotspot,flush=True)

def defaultdict_data(path):
    counts={}
    for line in path.open():
        if not line.startswith('pfc_switch '):
            continue
        _,sw,group,ports,n,ticks=line.split()
        flag,ports,n,ticks=map(int,(group,ports,n,ticks))
        c=counts.setdefault(sw,Counter())
        if ports:
            c['busy']+=n
            c['busy_ticks']+=ticks
            c['paused']+=n if flag&2 else 0
            c['paused_ticks']+=ticks if flag&2 else 0
            c['multi']+=n if ports>1 else 0
            c['parallel']+=n if flag&1 else 0
            c['other']+=n if flag&4 else 0
            c['overlap']+=n if flag&6==6 else 0
        c['max_ports']=max(c['max_ports'],ports)
    return counts

def main():
    output=Path(sys.argv[1]).resolve()
    ws=Path(sys.argv[2]).resolve()
    output.mkdir(parents=True,exist_ok=True)
    source=ws/'twins-paper-release-20261122'
    boundary_test(output,source)
    compiled=build(output,source,'HIP')
    with Path('/tmp/specdes-pfc-and-replay-hip.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        check_pressure(output/'measurements',ws,compiled)

if __name__=='__main__':
    main()
