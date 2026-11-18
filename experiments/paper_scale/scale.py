"""Run a paired CPU/GPU scale setting. Keep immutable trials and partial failures."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import threading
from collections import Counter
from itertools import zip_longest
from statistics import median

def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def run(cmd,d,name,env,timeout,core=None):
    if core is not None:cmd=['taskset','-c',str(core),*cmd]
    cmd=list(map(str,cmd))
    start=time.monotonic_ns()
    with (d/(name+'.log')).open('wb') as log:
        with subprocess.Popen(cmd,cwd=d,env={**os.environ,**env},stdout=log,stderr=subprocess.STDOUT,start_new_session=True) as proc:
            expired=threading.Event()
            def timeout_trial():
                if proc.poll() is None:
                    expired.set()
                    try:os.killpg(proc.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
            timer=threading.Timer(timeout,timeout_trial)
            timer.start()
            try:rc=proc.wait()
            finally:
                timer.cancel()
                timer.join()
            if expired.is_set():rc=124
    v=dict(command=cmd,rc=rc,wall_ns=time.monotonic_ns()-start)
    (d/(name+'.json')).write_text(json.dumps(v,indent=2))
    return v

def verify(d,flows,tcp):
    with flows.open() as f: inputs=list(csv.DictReader(f))
    if tcp:
        with (d/'reference.fct').open() as a,(d/'gpu.fct').open() as b:
            left,right=csv.reader(a),csv.reader(b)
            next(left);next(right); count=0
            for i,(x,y) in enumerate(zip_longest(left,right)):
                if not x or not y or x[:6]!=y[:6] or int(x[5])<0:raise ValueError(('flow',i,x,y))
                count+=1
    else:
        from irn_compare import flow_ids
        ids=flow_ids(flows)
        expected=[]
        for row in (d/'reference.fct').read_text().splitlines():
            x=list(map(int,row.split()));expected.append((ids[tuple(x[:6])],x[5],x[6]))
        observed=[tuple(map(int,row.split())) for row in (d/'gpu.fct').read_text().splitlines()]
        if len({row[0] for row in expected})!=len(ids) or len({row[0] for row in observed})!=len(ids):raise ValueError('missing or duplicated IRN flow IDs')
        if Counter(expected)!=Counter(observed):raise ValueError(('FCT mismatch',list((Counter(expected)-Counter(observed)).items())[:4]))
        count=len(observed)
    if count!=len(inputs):raise ValueError(('incomplete',count,len(inputs)))
    log=(d/'gpu.log').read_text()
    if tcp:
        for k in ('tx-cap','active-ovf','ooo-ovf','due-ovf','rtt-ovf','ring-ovf','undecided-ties'):
            if re.findall(k+r' (\d+)',log)[-1]!='0':raise ValueError(k)
    elif 'undecided ties 0, on lost tails 0 | overflows: tx 0 queue 0 ackq 0 qps 0 due 0' not in log:raise ValueError('IRN terminal diagnostics')
    return count

def main():
    p=argparse.ArgumentParser()
    for arg in ('inputs','out','build','tcp_ref','irn_tree'):p.add_argument(arg,type=Path)
    p.add_argument('--hosts',type=int,required=True);p.add_argument('--mode',choices=['heterogeneous','uniform','incast','spread'],required=True)
    p.add_argument('--trials',type=int,default=4);p.add_argument('--core',type=int);p.add_argument('--timeout',type=int,default=3600);p.add_argument('--image');p.add_argument('--irn-reference',type=Path);p.add_argument('--compat-libs',type=Path)
    a=p.parse_args()
    a.out=a.out.resolve(); a.out.mkdir(parents=True,exist_ok=True)
    src=a.inputs.resolve()/str(a.hosts);tcp=a.mode in ('heterogeneous','uniform'); inputs=src/('tcp' if tcp else a.mode)
    build=a.build.resolve();ref=a.tcp_ref.resolve();tree=a.irn_tree.resolve();tx=build/('twins' if tcp else 'twins-rdma')
    td=a.out/'fabric.txt'
    if not tcp:shutil.copyfile(src/'fabric.txt',td)
    attempts=[]
    for i in range(a.trials):
        d=a.out/f'trial-{i}';d.mkdir()
        flows=inputs/'flows.csv'
        if tcp:
            common=[f'--topofile={src}/{a.mode}.graph',f'--flows={flows}','--ecnK=30']
            cmds={'reference':[ref,'--topo=file','--hostq=red','--tcp=ns3::TcpDctcp','--flowEcmp=1','--stop=0.1',*common,'--out=reference.fct'],
                  'gpu':[tx,'--topo=graph','--tcp=dctcp','--spray=0',*common,'--out=gpu.fct']}
            env={}
        else:
            for n in ('config','ref.flows'):shutil.copyfile(inputs/n,d/n)
            oracle=a.irn_reference.resolve() if a.irn_reference else tree/'build/scratch/network-load-balance'
            env={'LD_LIBRARY_PATH':os.pathsep.join([str(tree/'build'),str(a.compat_libs.resolve())] if a.compat_libs else [str(tree/'build')]), 'IRN_BDP_BYTES':'156000'}
            rc=[oracle,'config']
            if a.image:
                rc=['docker','run','--rm','--network=none','--user',f'{os.getuid()}:{os.getgid()}','-v',f'{tree}:/src:ro','-v',f'{a.out}:/cases','-w',f'/cases/{d.name}','-e','LD_LIBRARY_PATH=/src/build',a.image,'/src/build/scratch/network-load-balance','config'];env={}
            cmds={'reference':rc,'gpu':[tx,'config',f'--topo={src}/uniform.graph',f'--flows={flows}','--out=gpu.fct','IRN_BDP=156000']}
        try:
            for name in (('reference','gpu') if i%2==0 else ('gpu','reference')):
                v=run(cmds[name],d,name,env if name=='reference' else {},a.timeout,a.core if name=='reference' else None)
                if v['rc']:raise ValueError((name,v['rc']))
            count=verify(d,flows,tcp)
        except Exception as e:
            (a.out/'failure.json').write_text(json.dumps(dict(trial=i,reason=str(e))));raise
        attempts.append({k:json.loads((d/(k+'.json')).read_text())['wall_ns'] for k in cmds})
        (d/'accepted.json').write_text(json.dumps(dict(flows=count,reference=sha(d/'reference.fct'),gpu=sha(d/'gpu.fct'))))
    stats={k:dict(median_ns=median(v[k] for v in attempts[1:]),range_ns=[min(v[k] for v in attempts[1:]),max(v[k] for v in attempts[1:])]) for k in cmds} if len(attempts)>1 else {}
    (a.out/'result.json').write_text(json.dumps(dict(flows=count,trials=attempts,measured=stats,executable_sha256=sha(tx),reference_sha256=sha(ref if tcp else oracle),input_sha256=sha(flows),single_core=a.core),indent=2))
if __name__=='__main__':
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'validation/extensions'))
    main()
