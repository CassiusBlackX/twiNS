"""Measure PFC occupancy and lineage decisions in an isolated counter build."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

def patch(path, old, new):
    source=path.read_text()
    if source.count(old)!=1:
        raise ValueError(f'unexpected code in {path}: {old}')
    path.write_text(source.replace(old,new,1))

def build(output,source,backend):
    tree=output/'source'
    tree.mkdir()
    for name in ('src','validation'):
        shutil.copytree(source/name,tree/name)
    shutil.copy2(source/'CMakeLists.txt',tree)
    (tree/'out').mkdir()
    (tree/'out/astra').symlink_to((source/'out/astra').resolve(),target_is_directory=True)
    kernel=tree/'src/rdma/rstep.cuh'
    driver=tree/'src/rdma/rmain.cu'
    header=tree/'src/rdma/rdma.h'
    patch(header,'    unsigned long long *irn_counts;', '    unsigned long long *strict_switch_counts;\n    unsigned long long *irn_counts;')
    patch(kernel,'namespace rdma\n{',
          'namespace rdma\n{\n__device__ unsigned long long g_strict_history[2][37];')
    patch(kernel,'    int ord = 0;\n    RunCursor A, B;',
          '    unsigned int strict_walk = 0;\n'
          '    struct StrictWalkRecorder {\n'
          '        unsigned int &count; unsigned long long *histogram;\n'
          '        __device__ ~StrictWalkRecorder() {\n'
          '            const int bucket = count ? 32 - __clz(count) : 0;\n'
          '            atomicAdd(histogram + 4 + bucket, 1ull);\n'
          '        }\n'
          '    } strict_recorder{strict_walk, g_strict_history[a.root == b.root]};\n'
          '    int ord = 0;\n    RunCursor A, B;')
    patch(kernel,'#ifdef RDMA_PROF\n        if (steps)\n            ++*steps;\n#endif',
          '        ++strict_walk;\n#ifdef RDMA_PROF\n        if (steps)\n            ++*steps;\n#endif')
    patch(kernel,'if (A.i > RUNS && B.i > RUNS && A.node == B.node && A.left == B.left)\n            return by_ord();',
          'if (A.i > RUNS && B.i > RUNS && A.node == B.node && A.left == B.left)\n'
          '        {\n            atomicAdd(g_strict_history[a.root == b.root] + 2, 1ull);\n            return by_ord();\n        }')
    token='if ((v & ~3ull) == w2 && e.w0 == A.node && e.w1 == B.node)\n                    {'
    patch(kernel,token,token+'\n                        atomicAdd(g_strict_history[a.root == b.root] + 3, 1ull);')
    patch(kernel,'    atomicAdd(&g_prof[0], 1ull);\n#endif',
          '    atomicAdd(&g_prof[0], 1ull);\n#endif\n    atomicAdd(g_strict_history[a.root == b.root], 1ull);')
    patch(kernel,'            atomicAdd(&s.ev[35], 1ull);',
          '            atomicAdd(&s.ev[35], 1ull);\n            atomicAdd(g_strict_history[0] + 1, 1ull);')
    patch(kernel,'    if (nsl > 1 && !safe)\n        return 2;',
          '''    unsigned long long *strict_stats = nullptr;
    unsigned long long strict_begin = 0;
    if (lane == 0 && !s.si[x].nvs && nsl == 1)
    {
        unsigned int active_ports = 0;
        bool paused = false;
        bool incoming_pause = false;
        unsigned long long arriving = 0;
        for (int p=0; p<deg; ++p)
        {
            const Port &port = w[p].pt;
            active_ports += ocnt[p] || (port.tc_at >= 0 && port.tc_at < t1) || port.q3_n || port.q0_n;
            paused |= port.mmu_paused || port.hdrm;
            incoming_pause |= port.paused;
            arriving += abytes[p];
        }
        const auto &sw = s.sw[x];
        long long room = (long long)WP.buffer_size - sw.total_hdrm - sw.total_rsrv - sw.shared_used - arriving;
        long long threshold = room > 0 ? room >> sw.max_shift : -1;
        if (sw.fixed_pfc_limit) threshold = sw.fixed_pfc_limit;
        bool other = !pooled || total > LK || D > 65535 || deg > lanes || (!WIDE && deg > SAFE_LANES);
        for (int p=0; p<deg; ++p)
        {
            long long used = (long long)w[p].pt.ing + abytes[p] - RESERVE;
            used = used > 0 ? used : 0;
            other |= used >= threshold || (abytes[p] > s.pc[s.pbase[x]+p].hdrm_cap && used+abytes[p] > threshold);
        }
        const int group = (safe ? 1 : 0) | (paused ? 2 : 0) | (other ? 4 : 0) | (incoming_pause ? 8 : 0);
        strict_stats = s.strict_switch_counts + ((size_t)x*16 + group)*258 + active_ports*2;
        atomicAdd(strict_stats, 1ull);
        strict_begin = clock64();
    }
    if (nsl > 1 && !safe)
        return 2;''')
    patch(kernel,'    return act ? 1 : 0;\n}\n\n// ------------------------------------------------------------------ the slot kernel',
          '    if (strict_stats) atomicAdd(strict_stats + 1, clock64() - strict_begin);\n'
          '    return act ? 1 : 0;\n}\n\n// ------------------------------------------------------------------ the slot kernel')
    patch(driver,'    s.qp = dev_alloc<SndQP>(F);',
          '    s.strict_switch_counts = dev_alloc<unsigned long long>((size_t)s.S * 16 * 258);\n    s.qp = dev_alloc<SndQP>(F);')
    patch(driver,'    if (s.irn_counts)\n    {',
          '''    {
        vector<unsigned long long> counts((size_t)s.S * 16 * 258);
        CK(cudaMemcpy(counts.data(), s.strict_switch_counts, counts.size()*sizeof(counts[0]), cudaMemcpyDeviceToHost));
        for (int x=0; x<s.S; ++x)
            for (int group=0; group<16; ++group)
                for (int ports=0; ports<=128; ++ports)
                {
                    auto index = ((size_t)x*16 + group)*258 + ports*2;
                    if (counts[index])
                        printf("pfc_switch %d %d %d %llu %llu\\n", x,group,ports,counts[index],counts[index+1]);
                }
        unsigned long long history[2][37];
        CK(hipMemcpyFromSymbol(history,HIP_SYMBOL(g_strict_history),sizeof(history)));
        for (int root=0; root<2; ++root)
            for (int counter=0; counter<37; ++counter)
                printf("root_history %d %d %llu\\n",root,counter,history[root][counter]);
    }
    if (s.irn_counts)
    {'''.replace('llu\n"',r'llu\n"'))
    for name in ('rdma.h','rstep.cuh','rmain.cu'):
        path=tree/'src/rdma'/name
        (output/(name+'.sha256.log')).write_text(hashlib.sha256(path.read_bytes()).hexdigest()+'\n')
    ninja=output/'compiled'
    flags=['-DTWINS_GPU_BACKEND=HIP','-DCMAKE_HIP_ARCHITECTURES=gfx1200','-DCMAKE_HIP_COMPILER=/opt/rocm/llvm/bin/clang++'] if backend=='HIP' else [
        '-DTWINS_GPU_BACKEND=CUDA','-DCMAKE_CUDA_ARCHITECTURES=120','-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc']
    with (output/'build.log').open('wb') as log:
        subprocess.run(['cmake','-S',tree,'-B',ninja,'-G','Ninja','-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',*flags],
                       stdout=log,stderr=subprocess.STDOUT,check=True)
        subprocess.run(['cmake','--build',ninja,'--target','twins-ai','twins-astra-stub','-j2'],
                       stdout=log,stderr=subprocess.STDOUT,check=True)
    shutil.copy2(ninja/'compile_commands.json',output)
    return ninja

def normalized(path):
    return sorted(line.strip() for line in path.read_text().splitlines() if line.strip())

def measure(out, build_dir, source, data, clean=None):
    out.mkdir(parents=True)
    opts=['--gwin=0','--nvl-islands=0','--fp-bases=1000003,1000033']
    cfg=source/'validation/simai/SimAI.conf'
    base=data/'out/simai/wl/paper/c_alltoall_64m_128.txt'
    trials=[]
    cases=[('ag1024','hpn1024','c_allgather_512m_1024',None),
           ('gpt3probe','hpn128','gpt3_13b_probe_128',None),
           *[(f'dcn-burst-{m}m','dcn128',None,m) for m in (8,64,256)]]
    for label,topo,workload,megabytes in cases:
        case=out/label
        case.mkdir()
        if megabytes is not None:
            workload=case/'workload.log'
            workload.write_text(base.read_text().replace('67108864',str(megabytes*1024**2)))
        else:
            workload=data/f'out/simai/wl/paper/{workload}.txt'
        for arm in ('safe','serial') if megabytes is not None else ('safe',):
            binaries=[('diagnostic',build_dir/'twins-ai',1)] if clean is None else [
                ('diagnostic',build_dir/'twins-ai',1),('clean',clean,3)]
            for kind,binary,repeats in binaries:
                for i in range(repeats):
                    work=case/arm/kind/f'trial-{i}'
                    work.mkdir(parents=True)
                    cmd=[str(binary),str(cfg),f'--topo={data}/data/simai/{topo}.topo',f'--astra={workload}',
                         '--out=messages.fct','--dag-out=actions.dag',*opts]
                    if arm=='serial':
                        cmd.append('--serial')
                    start=time.monotonic_ns()
                    with (work/'process.log').open('wb') as log:
                        try:
                            rc=subprocess.run(cmd,cwd=work,stdout=log,stderr=subprocess.STDOUT,timeout=1800).returncode
                        except subprocess.TimeoutExpired:
                            rc=124
                        elapsed=time.monotonic_ns()-start
                    text=(work/'process.log').read_text(errors='replace')
                    row=dict(case=label,arm=arm,kind=kind,trial=i,rc=rc,wall_ns=elapsed,command=cmd,
                             hash=hashlib.sha256(binary.read_bytes()).hexdigest(),
                             pfc=re.findall(r'drops (\d+) marks (\d+) pauses (\d+) resumes (\d+)',text)[-1:],
                             switch_counter_rows=sum(x.startswith('pfc_switch ') for x in text.splitlines()),
                             history=[list(map(int,x.split()[1:])) for x in text.splitlines() if x.startswith('root_history ')])
                    frozen={'ag1024':'paper/c_allgather_512m_hpn1024','gpt3probe':'paper/gpt3_13b_probe_hpn128',
                            'dcn-burst-64m':'whatif/a2a_dcn'}.get(label)
                    fct=work/'messages.fct'
                    if fct.exists() and frozen:
                        row['reference_equal']=normalized(fct)==normalized(data/f'out/simai/{frozen}.ref')
                    if fct.exists():
                        row['fct_sha256']=hashlib.sha256(fct.read_bytes()).hexdigest()
                    (work/'receipt.json').write_text(json.dumps(row,indent=2)+'\n')
                    trials.append(row)
                    print(label,arm,kind,i,rc,row['pfc'],row.get('reference_equal'),flush=True)
    (out/'summary.json').write_text(json.dumps(trials,indent=2)+'\n')

def main():
    out=Path(sys.argv[1]).resolve()
    out.mkdir(parents=True,exist_ok=False)
    if len(sys.argv)>2:
        root=Path(sys.argv[2]).resolve()
        source=root/'twins-paper-release-20261122'
        data=root/'twiNS'
        bin_dir=build(out,source,'HIP')
        measure(out/'measurements',bin_dir,source,data)
        return
    videos=Path.home()/'Videos'
    source=videos/'twins-paper-followup-20261123/candidate'
    data=videos/'twins-release-cuda-20261122/project/paper-inputs'
    bin_dir=build(out,source,'CUDA')
    lock=videos/'twins-isolated-20261118/out/exclusive-campaign.lock'
    with lock.open('r+') as stream:
        import fcntl
        fcntl.flock(stream,fcntl.LOCK_EX)
        measure(out/'measurements',bin_dir,source,data,source/'build/twins-ai')

if __name__=='__main__':
    main()
