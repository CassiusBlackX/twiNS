"""Audit FatTree capacity and narrower queue storage on the RTX 5090."""

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import monotonic_ns,sleep

import pynvml as nv
from crosspod import CANDIDATE,LOCK,OBSERVER,ROOT,replace,sha
from pfc_sustained import save
from rdma_scale_reference import DATA,HOST_LOCK,TRACES_UNTIL


def build(out):
    out.mkdir()
    tree=out/'source'
    tree.mkdir()
    shutil.copytree(CANDIDATE/'src',tree/'src')
    shutil.copy2(CANDIDATE/'CMakeLists.txt',tree)
    main=tree/'src/rdma/rmain.cu'
    src=tree/'src/rdma/rstep.cuh'
    replace(main,'    CK(cudaMalloc((void **)&p, std::max<size_t>(n, 1) * sizeof(T)));', '''    const size_t bytes = std::max<size_t>(n, 1) * sizeof(T);
    extern unsigned long long strict_allocated;
    const auto code = cudaMalloc((void **)&p, bytes);
    if (code != cudaSuccess)
        fprintf(stderr, "strict_allocation_failed request_bytes=%zu successful_bytes=%llu\\n", bytes, strict_allocated);
    CK(code);
    strict_allocated += bytes;''')
    replace(main,'int main(int argc, char **argv)', 'unsigned long long strict_allocated = 0;\n\nint main(int argc, char **argv)')
    replace(main,'''        if (!build_world(worlds[0], base, topo_file, flow_file, bps, delay))
            return 1;''','''        const auto graph_start = std::chrono::steady_clock::now();
        if (!build_world(worlds[0], base, topo_file, flow_file, bps, delay))
            return 1;
        const auto graph_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                              std::chrono::steady_clock::now()-graph_start).count();
        uint64_t route_hash = 14695981039346656037ull;
        for (const auto &r : worlds[0].rt) {
            route_hash = (route_hash ^ r.off) * 1099511628211ull;
            route_hash = (route_hash ^ r.n) * 1099511628211ull;
        }
        for (const auto port : worlds[0].rtp)
            route_hash = (route_hash ^ port)*1099511628211ull;
        printf("strict_graph build_ns=%lld route_count=%zu route_bytes=%zu base_rtt=%llu win_bytes=%u route_fingerprint=%llu\\n",
               (long long)graph_ns, worlds[0].rt.size(), worlds[0].rtp.size(), (unsigned long long)worlds[0].P.base_rtt,
               worlds[0].P.win, (unsigned long long)route_hash);
        fflush(stdout);''')
    replace(main,'    printf("flows done %u of %d; simulated',
            '    printf("strict_requested_bytes=%llu\\n", strict_allocated);\n    printf("flows done %u of %d; simulated')
    replace(src,'    TxRec &r = s.tx[i];','    TxRec &r = s.tx[i];\n    r.index = i;')
    replace(src,'        r.index = i;\n','')
    replace(main,'return a.ts != b.ts ? a.ts < b.ts : a.node != b.node ? a.node < b.node : a.dev < b.dev;',
            'if (a.ts != b.ts) return a.ts < b.ts;\n            if (a.node != b.node) return a.node < b.node;\n'
            '            if (a.dev != b.dev) return a.dev < b.dev;\n            return a.index < b.index;')
    exe=out/'compiled/twins-rdma'
    cmds=[['cmake','-S',tree,'-B',exe.parent,'-G','Ninja','-DTWINS_GPU_BACKEND=CUDA',
           '-DCMAKE_CUDA_ARCHITECTURES=120','-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc'],
          ['cmake','--build',exe.parent,'--target','twins-rdma','-j2']]
    with (out/'build.log').open('wb') as stream:
        for cmd in cmds:
            subprocess.run(['taskset','-c','10,12',*cmd],stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=600)
    save(out/'build-provenance.json',{'binary_sha256':sha(exe),'source_hashes':{str(p.relative_to(tree)):sha(p) for p in (main,src)},
                                     'commands':[list(map(str,c)) for c in cmds]})


def rss_high(pid):
    try:
        m=re.search(r'^VmHWM:\s+(\d+)',Path(f'/proc/{pid}/status').read_text(),re.M)
        return int(m[1])*1024 if m else 0
    except FileNotFoundError:
        return 0


def main_run(out,case,binary,kind,cap,trace):
    out.mkdir(parents=True)
    spec=json.loads((case/'input.json').read_text())
    env=dict(os.environ,TWINS_TRACE_PORTS=str(case/'ports.log'))
    cmd=['taskset','-c','0',str(binary),str(case/'config.log'),f'--topo={case}/topo.log',f'--flows={case}/{kind}.csv',
         '--out=output.fct',f'--q3cap={cap}','CC_MODE=1','--fp-bases=1000003,1000033']
    if trace:
        cmd+=['--txtrace=selected-wire.log',f'--until={TRACES_UNTIL}']
    nv.nvmlInit()
    gpu=nv.nvmlDeviceGetHandleByIndex(0)
    maxrss,peak=0,0
    seen=0
    start=monotonic_ns()
    with (out/'process.log').open('wb') as log,subprocess.Popen(cmd,cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT) as p:
        while p.poll() is None:
            maxrss=max(maxrss,rss_high(p.pid))
            for r in nv.nvmlDeviceGetComputeRunningProcesses(gpu):
                if r.pid==p.pid and r.usedGpuMemory is not None:
                    peak=max(peak,r.usedGpuMemory);seen+=1
            if monotonic_ns()-start>410_000_000_000:
                p.kill();p.wait()
                break
            sleep(.015)
    elapsed=monotonic_ns()-start
    log=(out/'process.log').read_text()
    final=re.findall(r'flows done (\d+) of (\d+); simulated ([.\d]+) s in ([.\d]+) s wall \((\d+) launches\)',log)
    events=re.findall(r'drops (\d+) marks (\d+) pauses (\d+) resumes (\d+) nacks (\d+) \| undecided ties (\d+), on lost tails (\d+) \| overflows: tx (\d+) queue (\d+) ackq (\d+) qps (\d+) due (\d+)',log)
    graph=next((line for line in log.splitlines() if line.startswith('strict_graph ')),'')
    row=dict(k=spec['k'],traffic=kind,q3cap=cap,trace=trace,rc=p.returncode,wall_ns=elapsed,
             pid=p.pid,host_highwater_observed_bytes=maxrss,device_peak_observed_bytes=peak,device_pid_polls=seen,
             device_total_bytes=nv.nvmlDeviceGetMemoryInfo(gpu).total,
             graph={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',graph)},cmd=cmd,
             explicit_bytes=re.findall(r'strict_requested_bytes=(\d+)',log),
             allocation_failure=re.findall(r'strict_allocation_failed request_bytes=(\d+) successful_bytes=(\d+)',log),
             final=final[-1] if final else None,
             counts=dict(zip(('drops','marks','pauses','resumes','nacks','ties','lost','tx','queue','ackq','qps','due'),
                         map(int,events[-1]))) if events else None)
    nv.nvmlShutdown()
    fct=out/'output.fct'
    if fct.is_file(): row['fct_sha256']=sha(fct)
    if trace and (out/'selected-wire.log').is_file(): row['wire_sha256']=sha(out/'selected-wire.log')
    save(out/'receipt.json',row)
    print('scale CUDA',spec['k'],kind,cap,trace,row['rc'],row['final'],round(peak/2**30,2),flush=True)


def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        dest,case,binary=map(Path,sys.argv[2:5]); kind=sys.argv[5]
        return main_run(dest,case,binary,kind,int(sys.argv[6]),sys.argv[7]=='wire')
    out.mkdir()
    build(out/'build')
    binary=out/'build/compiled/twins-rdma'
    prior=next(p for p in sorted((ROOT/'rdma_scale_reference').glob('20*'),reverse=True) if (p/'results/inputs/k40/input.json').is_file())
    for k,kind,cap,flag in ((40,'incast',4096,'clean'),(40,'incast',2048,'clean'),(40,'distributed',2048,'clean'),
                            (40,'incast',2048,'wire'),(40,'distributed',2048,'wire'),(40,'incast',1024,'wire'),
                            (40,'distributed',1024,'wire'),(36,'incast',4096,'wire'),(36,'distributed',4096,'wire')):
        case=prior/'results/inputs'/f'k{k}'
        dest=out/'measurements'/f'k{k}'/kind/f'q{cap}-{flag}'
        with HOST_LOCK.open('a') as mem:
            fcntl.flock(mem,fcntl.LOCK_EX)
            subprocess.run(['flock',LOCK,sys.executable,OBSERVER,out/'resources'/f'k{k}-{kind}-{cap}-{flag}',
                            sys.executable,__file__,out,dest,case,binary,kind,str(cap),flag],check=True)


if __name__=='__main__':
    main()
