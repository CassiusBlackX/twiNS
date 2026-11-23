"""Rebuild all linked ns-3 protocol libraries with a controlled FMA setting."""
import csv
from collections import defaultdict
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

VIDEOS=Path.home()/'Videos'
ORIGINAL=VIDEOS/'netsim/unison'
CANDIDATE=VIDEOS/'twins-paper-followup-20261123/candidate'
INPUT=VIDEOS/'twins-paper-followthrough-20261122/project/data-input/tcp/128'
LOCK=VIDEOS/'twins-isolated-20261118/out/exclusive-campaign.lock'

def sha(p):
    with p.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def build(root,mode):
    work=root/'build-receipts'/mode
    work.mkdir(parents=True)
    tree=root.parent/f'ns3-{mode}'
    shutil.copytree(ORIGINAL,tree,symlinks=True,ignore=shutil.ignore_patterns(
        'build','cmake-cache','.git','.lock-ns3_linux_build','out'))
    scenario=tree/'scratch/twins-ref.cc'
    shutil.copy2(CANDIDATE/'validation/ns3/twins-ref.cc',scenario)
    builddir=tree/'cmake-recheck'
    config=['cmake','-S',tree,'-B',builddir,'-G','Ninja','-DCMAKE_BUILD_TYPE=release',
        '-DCMAKE_CXX_COMPILER=/usr/bin/c++',f'-DCMAKE_CXX_FLAGS=-ffp-contract={mode} -I{CANDIDATE}/src',
        '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON','-DNS3_MTP=ON','-DNS3_NATIVE_OPTIMIZATIONS=ON',
        '-DNS3_EXAMPLES=OFF','-DNS3_TESTS=OFF','-DNS3_ENABLE_BUILD_VERSION=OFF','-DNS3_WARNINGS_AS_ERRORS=OFF']
    command=['cmake','--build',builddir,'--target','scratch_twins-ref','-j4']
    (work/'commands.json').write_text(json.dumps(dict(configuration=list(map(str,config)),build=list(map(str,command))),indent=2)+'\n')
    with (work/'build.log').open('wb') as log:
        subprocess.run(config,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=900)
        subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=10800)
    binary=tree/'build/scratch/ns3.46.1-twins-ref-optimized'
    if not binary.is_file():
        raise ValueError(f'unknown executable name: {list((tree/"build/scratch").glob("*twins*"))}')
    files=list((tree/'build/lib').glob('libns3*.so'))
    if len(files)<6:
        raise ValueError('expected a rebuilt protocol library set')
    shutil.copy2(builddir/'compile_commands.json',work)
    (work/'build-provenance.json').write_text(json.dumps(dict(binary_sha256=sha(binary),
        input_source_sha256=sha(scenario),libraries={p.name:sha(p) for p in files}),indent=2)+'\n')
    return binary

def invoke(work,bin,topo,proto):
    work.mkdir(parents=True)
    fct=work/'output.fct'
    if proto=='gpu':
        raise ValueError('expected a protocol')
    if bin.name=='twins':
        args=[bin,'--topo=graph',f'--tcp={proto.lower()}','--ecnK=30','--spray=0',
            f'--topofile={topo}',f'--flows={INPUT}/tcp/flows.csv',f'--out={fct}',
            f'--qtrace={work}/raw-queue.csv','--qtrace_until=20000000']
    else:
        args=[bin,'--topo=file','--hostq=red','--flowEcmp=1','--stop=0.1','--ecnK=30',
            f'--tcp=ns3::Tcp{proto}',f'--topofile={topo}',f'--flows={INPUT}/tcp/flows.csv',f'--out={fct}']
    import os
    log=work/'process.log'
    with log.open('wb') as stream:
        result=subprocess.run(['taskset','-c','3',*map(str,args)],cwd=work,
            env={**os.environ,'TRACE_ALLQ':'20000000'},stdout=stream,stderr=subprocess.STDOUT,timeout=900)
    return dict(rc=result.returncode,command=list(map(str,args)),binary_sha256=sha(bin))

def read(work):
    f=work/'output.fct'
    flows=[tuple(map(int,row[:6])) for row in list(csv.reader(f.open()))[1:]] if f.is_file() else []
    native=(work/'raw-queue.csv').is_file()
    rows=defaultdict(list)
    for line in (work/('raw-queue.csv' if native else 'process.log')).open():
        if native or line.startswith('Q '):
            row=line.split() if native else line.split()[1:]
            if len(row)>=8:
                rows[tuple(map(int,row[1:3]))].append([int(row[0]),int(row[3]),int(row[4]),row[5],int(row[6]),int(row[7])])
    if native:
        for port in rows:
            rows[port].sort(key=lambda row:row[0])
    return flows,rows

def compare(out,l,r):
    left,a=read(l)
    right,b=read(r)
    row=next((dict(index=i,first=x,second=y) for i,(x,y) in enumerate(zip(left,right)) if x!=y),None)
    changes=[]
    for port in sorted(a.keys()|b.keys()):
        x,y=a[port],b[port]
        if x!=y:
            index=next((i for i,(p,q) in enumerate(zip(x,y)) if p!=q),min(len(x),len(y)))
            changes.append(dict(port=port,index=index,first=x[index] if index<len(x) else None,
                                second=y[index] if index<len(y) else None))
    return dict(fct_equal=bool(left) and left==right,lengths=[len(left),len(right)],
                first_fct_difference=row,queue_rows=[sum(map(len,a.values())),sum(map(len,b.values()))],
                queue_equal=bool(a) and a==b,differing_ports=len(changes),first_queues=changes[:5])

def main():
    out=Path(sys.argv[1]).resolve()
    out.mkdir(parents=True)
    variants={mode:build(out,mode) for mode in ('off','fast')}
    graph_dir=out/'inputs'
    graph_dir.mkdir()
    standard=graph_dir/'heterogeneous.graph'
    fractional=graph_dir/'fractional.graph'
    shutil.copy2(INPUT/'heterogeneous.graph',standard)
    lines=standard.read_text().splitlines()
    for i,text in enumerate(lines):
        if text.startswith('link '):
            fields=text.split()
            fields[4]=str(int(fields[4])+137)
            lines[i]=' '.join(fields)
    fractional.write_text('\n'.join(lines)+'\n')
    cpu={}
    for graph in (standard,fractional):
        for protocol in ('Dctcp','Cubic'):
            key=graph.stem+'/'+protocol.lower()
            case=out/'cases'/key
            runs={mode:invoke(case/mode,bin,graph,protocol) for mode,bin in variants.items()}
            cpu[key]=dict(input_sha256=sha(graph),runs=runs,result=compare(case,case/'off',case/'fast'))
            (out/'cpu-summary.json').write_text(json.dumps(cpu,indent=2)+'\n')
    lock=LOCK.open('a')
    with lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        for graph in (standard,fractional):
            for protocol in ('Dctcp','Cubic'):
                key=graph.stem+'/'+protocol.lower()
                case=out/'cases'/key
                run=invoke(case/'cuda',CANDIDATE/'build/twins',graph,protocol)
                cpu[key]['cuda']=dict(run=run,result=compare(case,case/'off',case/'cuda'))
                (out/'summary.json').write_text(json.dumps(cpu,indent=2)+'\n')

if __name__=='__main__':
    main()
