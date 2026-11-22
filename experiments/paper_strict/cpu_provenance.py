"""Recover actual CPU artifacts and compare simulator, fresh-container, and live-container timings."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

VIDEOS=Path.home()/'Videos'
NET=VIDEOS/'netsim'
ISO=VIDEOS/'twins-isolated-20261118'
SIM='astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation'
HPCC=NET/'hpcc'
RDMA=NET/'rdma'

def output(argv):
    x=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    return dict(rc=x.returncode,output=x.stdout)

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

def write(path,data):
    path.write_text(json.dumps(data,indent=2)+'\n')

def provenance(root):
    models={
      'fig5-original':(NET/'unison/build/scratch/ns3.46.1-twins-ref-optimized',NET/'unison/cmake-cache/CMakeCache.txt'),
      'fig5-cubic':(ISO/'tcp-reference/ns3-unison-ref',NET/'unison/cmake-cache/CMakeCache.txt'),
      'hpcc-k32':(HPCC/'simulation/build/scratch/rdma-ref',HPCC/'simulation/build/c4che/_cache.py'),
      'simai-t64':(NET/'simai-t64'/SIM/'build/scratch/ns3.36.1-AstraSimNetwork-optimized',
                   NET/'simai-t64'/SIM/'cmake-cache/CMakeCache.txt'),
      'simai-mtp':(ISO/'simai-mtp'/SIM/'build-mtp/scratch/ns3.36.1-AstraSimNetwork-optimized',
                   ISO/'simai-mtp'/SIM/'cmake-mtp/CMakeCache.txt'),
    }
    data={}
    for name,(binary,cache) in models.items():
        row=dict(path=str(binary),found=binary.is_file(),cache_path=str(cache),cache_present=cache.is_file())
        if binary.is_file():
            row.update(sha256=sha(binary),elf_sections=output(['readelf','-W','-S',binary]),
                       compiler_comment=output(['readelf','-p','.comment',binary]),
                       isa_notes=output(['readelf','-W','-n',binary]),
                       dynamic_dependencies=output(['readelf','-W','-d',binary]))
        if cache.is_file():
            row['cache_sha256']=sha(cache)
            text=cache.read_text(errors='replace')
            row['optimization_cache_lines']=[line for line in text.splitlines() if re.match(
                r'(?:CMAKE_(?:BUILD_TYPE|CXX_COMPILER|CXX_FLAGS|INTERPROCEDURAL_OPTIMIZATION)|NS3_.*(?:NATIVE|MTP)|(?:CXX|CXXFLAGS|CFLAGS|LINKFLAGS|BUILD_PROFILE|COMPILER_CXX)\s*=)',line)]
        data[name]=row
    write(root/'binaries.json',data)
    return data

def timer(root):
    img='localhost/hpcc-build'
    case_def=(('incast16',1),('a2a50s',3))
    shell='start=$(date +%s%N); "$@"; rc=$?; end=$(date +%s%N); printf "\\ninternal_wall_ns %s\\n" "$((end-start))"; exit "$rc"'
    rows=[]
    for repeat in range(3):
        order=('fresh','live') if repeat%2==0 else ('live','fresh')
        for traffic,cc in case_def:
            name=f'k32_{traffic}_cc{cc}'
            for mode in order:
                dest=root/name/f'{mode}-{repeat}'
                dest.mkdir(parents=True)
                container=f'twins-cpu-timer-{root.name.lower()}-{traffic}-{mode}-{repeat}'
                mounts=['docker','run','--rm','--network=none','--name',container,
                        '--user',f'{os.getuid()}:{os.getgid()}','--cpuset-cpus','4','--memory=70g','--memory-swap=70g',
                        '-v',f'{HPCC}:/hpcc:ro','-v',f'{RDMA}:/inputs:ro','-v',f'{dest}:/result','-w','/result',
                        '-e','LD_LIBRARY_PATH=/hpcc/simulation/build']
                args=['bash','-c',shell,'_','/hpcc/simulation/build/scratch/rdma-ref','/inputs/hpcc.cfg',
                      '--topo=/inputs/ft32.topo',f'--flows=/inputs/k32_{traffic}.csv',
                      '--out=/result/output.fct',f'CC_MODE={cc}']
                if mode=='live':
                    subprocess.run([*mounts,'-d',img,'sleep','3600'],stdout=subprocess.DEVNULL,check=True)
                    cmd=['docker','exec',container,*args]
                else:
                    cmd=[*mounts,img,*args]
                start=time.monotonic_ns()
                try:
                    with (dest/'process.log').open('wb') as log:
                        try:
                            process=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=2400)
                            rc=process.returncode
                        except subprocess.TimeoutExpired:
                            rc=124
                        elapsed=time.monotonic_ns()-start
                finally:
                    if mode=='live' or locals().get('rc')==124:
                        subprocess.run(['docker','stop','-t','4',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                wall=re.findall(r'internal_wall_ns (\d+)',(dest/'process.log').read_text(errors='replace'))
                result=dest/'output.fct'
                ref=RDMA/(name+'.fct')
                equal=result.is_file() and sorted(result.read_bytes().splitlines())==sorted(ref.read_bytes().splitlines())
                row=dict(case=name,trial=repeat,mode=mode,command=cmd,rc=rc,outer_wall_ns=elapsed,
                         simulator_wall_ns=int(wall[-1]) if wall else None,equal=equal,
                         reference_sha256=sha(ref),output_sha256=sha(result) if result.is_file() else None)
                write(dest/'receipt.json',row)
                rows.append(row)
                print(name,mode,repeat,rc,equal,flush=True)
    write(root/'timing-summary.json',dict(rows=rows,
        scope='Fresh means new container from local image; live means exec into an already started container. No system page-cache flushing.'))

def main():
    root=Path(sys.argv[1]).resolve()
    root.mkdir(parents=True)
    if len(sys.argv)>2:
        timer(root)
        return 0
    provenance(root)
    videos=VIDEOS/'twins-paper-strict-20261123'
    deadline=time.monotonic()+4200
    while time.monotonic()<deadline:
        u=sorted((videos/'unison').glob('20*'))
        x=sorted((videos/'crosspod').glob('20*'))
        if u and x and (u[-1]/'results/measurements/summary.json').exists() and (x[-1]/'results/measurements/summary.json').exists():
            break
        time.sleep(15)
    lock=ISO/'out/exclusive-campaign.lock'
    return subprocess.run(['flock',str(lock),sys.executable,__file__,str(root/'timing'),'run']).returncode

if __name__=='__main__':
    raise SystemExit(main())
