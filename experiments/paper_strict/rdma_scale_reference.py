"""Freeze FatTree scale inputs and run independent sequential HPCC under a memory bound."""

from collections import Counter
import csv
import fcntl
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
from time import monotonic_ns

from crosspod import ROOT, sha
from pfc_sustained import save


HOME=Path.home()/'Videos/netsim'
DATA=HOME/'rdma'
MODEL=HOME/'hpcc'
HOST_LOCK=ROOT/'scale-heavy-host.lock'
TRACES_UNTIL=2_000_000


def config(out,k):
    h=k**3//4
    pod=(k//2)**2
    nports=5*h
    out.mkdir(parents=True)
    with (out/'topo.log').open('wb') as stream:
        subprocess.run([sys.executable,Path(__file__).with_name('topo.py'),'fattree',str(k)],stdout=stream,check=True)
    shutil.copy2(DATA/'hpcc.cfg',out/'config.log')
    def write(kind,rows):
        fn=out/f'{kind}.csv'
        with fn.open('w') as stream:
            w=csv.writer(stream,lineterminator='\n')
            w.writerow(('src','dst','flow_size','start_ns'))
            w.writerows(rows)
        return dict(rows=len(rows),sources=len({r[0] for r in rows}),destinations=len({r[1] for r in rows}),
                    active=len({f for r in rows for f in r[:2]}),sha256=sha(fn))
    rng=random.Random(25032)
    order=list(range(h)); rng.shuffle(order)
    distributed=[]
    for i,src in enumerate(order):
        p,j=divmod(src,pod)
        dst=((p+1+j%(k-1))%k)*pod+(j+1)%pod
        distributed.append((src,dst,16384,1000+50*i))
    assert len({row[1] for row in distributed})==h
    candidates=list(range(pod,h));rng.shuffle(candidates)
    incast=[(src,0,93440,100000+100000*(i//16)) for i,src in enumerate(candidates[:320])]
    manifest={'k':k,'hosts':h,'switch_ports':nports,'config_sha256':sha(out/'config.log'),
              'topo_sha256':sha(out/'topo.log'), 'tx_until_ns':TRACES_UNTIL,'selected_ports':[[h,1],[h,k//2+1]],
              'traffic':{'distributed':write('distributed',distributed),'incast':write('incast',incast)}}
    save(out/'input.json',manifest)
    (out/'ports.log').write_text(''.join(f'{a} {b}\n' for a,b in manifest['selected_ports']))


def resources(out):
    other32=18315420664-5*8192*4096*64
    alloc=[]
    for k in (32,36,40,62):
        hosts=k**3//4; ports=5*hosts
        alloc.append(dict(k=k,hosts=hosts,ports=ports,q3_default_bytes=ports*4096*64,
              q3_2048_bytes=ports*2048*64, q3_1024_bytes=ports*1024*64,route_scratch_bytes=28*hosts**2,
              projected_linear_other_device_bytes=round(other32*hosts/8192)))
    save(out/'resource-projection.json',alloc)


def run_reference(case,kind,dest,core=4,memory='70g'):
    dest.mkdir(parents=True)
    test=json.loads((case/'input.json').read_text())
    which=f"twins-scale-k{test['k']}-{kind}-{os.getpid()}"
    script='"$@"; rc=$?; printf "cgroup_memory_peak_bytes="; cat /sys/fs/cgroup/memory.peak; cat /sys/fs/cgroup/memory.events; exit "$rc"'
    cmd=['docker','run','--rm','--name',which,'--network=none','--ulimit','core=0','--cpuset-cpus',str(core),
         f'--memory={memory}',f'--memory-swap={memory}','--user',f'{os.getuid()}:{os.getgid()}',
         '-v',f'{MODEL}:/hpcc:ro','-v',f'{case}:/case:ro','-v',f'{dest}:/result','-w','/result',
         '-e','LD_LIBRARY_PATH=/hpcc/simulation/build','--entrypoint','/bin/bash','localhost/hpcc-build','-c',script,'_',
         '/hpcc/simulation/build/scratch/rdma-ref','/case/config.log','--topo=/case/topo.log',f'--flows=/case/{kind}.csv',
         '--out=/result/output.fct','--txtrace=/result/wire.raw',f'--until={TRACES_UNTIL}','CC_MODE=1']
    save(dest/'started.json',{'command':cmd,'inputs':test})
    t=monotonic_ns()
    with (dest/'process.log').open('wb') as log:
        try:
            rc=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=1500).returncode
        except subprocess.TimeoutExpired:
            subprocess.run(['docker','stop','-t','4',which],stdout=log,stderr=subprocess.STDOUT,timeout=30)
            rc=124
    elapsed=monotonic_ns()-t
    log=(dest/'process.log').read_text()
    wire=dest/'wire.raw'
    detail=dict(k=test['k'],traffic=kind,wall_ns=elapsed,rc=rc,binary_sha256=sha(MODEL/'simulation/build/scratch/rdma-ref'),
                memory_peak_bytes=[int(x) for x in re.findall(r'cgroup_memory_peak_bytes=(\d+)',log)],
                oom_kill=re.findall(r'^oom_kill (\d+)',log,re.M),command=cmd)
    if wire.is_file():
        ports={tuple(map(str,x)) for x in test['selected_ports']}
        with wire.open() as stream,(dest/'selected-wire.log').open('w') as out:
            for row in stream:
                x=row.split()
                if len(x)==10 and tuple(x[1:3]) in ports:
                    out.write(row)
        detail['full_trace_sha256']=sha(wire)
    fct=dest/'output.fct'
    if fct.is_file():
        detail['flows']=sum(1 for _ in fct.open())
    save(dest/'receipt.json',detail)
    print('HPCC',test['k'],kind,rc,detail.get('flows'),detail['memory_peak_bytes'],flush=True)
    return detail


def main():
    out=Path(sys.argv[1]).resolve()
    out.mkdir()
    resources(out)
    for k in (32,36,40):
        config(out/'inputs'/f'k{k}',k)
    rows=[]
    for k in (40,36):
        case=out/'inputs'/f'k{k}'
        with HOST_LOCK.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            # A route-build OOM applies to both traffic arms of the same topology.
            for kind in ('incast','distributed'):
                row=run_reference(case,kind,out/'reference'/f'k{k}'/kind)
                rows.append(row)
                save(out/'summary.json',rows)
                if row['oom_kill'] and int(row['oom_kill'][-1])>0:
                    break
        if all(x['rc']==0 and x.get('flows')==x.get('traffic_count',320 if x['traffic']=='incast' else k**3//4)
               for x in rows if x['k']==k) and len([r for r in rows if r['k']==k])==2:
            break


if __name__=='__main__':
    main()
