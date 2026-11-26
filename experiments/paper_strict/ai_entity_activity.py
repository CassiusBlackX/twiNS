"""Measure the physical NVLink/fabric union and device steps in all three window modes."""

from pathlib import Path
import re
import shutil
import subprocess
import sys

from ai_entity_timing import CASES,ARMS,sample
from crosspod import CANDIDATE,LOCK,OBSERVER,replace,sha
from pfc_sustained import save


CAP_NS=1_000_000_000
MARK='''__device__ __forceinline__ void strict_link_busy(const Sim &s, int link, int64_t now, int64_t tx)
{
    if (!tx) return; // a zero-duration frame has no serialization interval
    const unsigned long long a = fdiv(s.fD, now), b = fdiv(s.fD, now + tx - 1);
    const size_t first = a >> 6, last = b >> 6;
    if (last >= s.strict_words)
    {
        atomicAdd(s.strict_steps+2, 1ull);
        return;
    }
    auto *bits = s.strict_activity + (s.strict_is_nvl[link] ? 0 : s.strict_words);
    const unsigned long long head = ~0ull << (a & 63), tail = ~0ull >> (63 - (b & 63));
    if (first == last)
        atomicOr(bits+first, head & tail);
    else
    {
        atomicOr(bits+first, head);
        for (size_t word = first+1; word < last; ++word)
            atomicOr(bits+word, ~0ull);
        atomicOr(bits+last, tail);
    }
}

'''


def build(out):
    out.mkdir()
    tree=out/'source'
    tree.mkdir()
    shutil.copytree(CANDIDATE/'src',tree/'src')
    shutil.copy2(CANDIDATE/'CMakeLists.txt',tree)
    (tree/'out').mkdir()
    (tree/'out/astra').symlink_to((CANDIDATE/'out/astra').resolve(),target_is_directory=True)
    d=tree/'src/rdma'; hdr=d/'rdma.h'; main=d/'rmain.cu'; ker=d/'rstep.cuh'
    replace(hdr,'    unsigned long long *ev; // counters (see main)', '''    unsigned long long *strict_steps;
    unsigned long long *strict_activity;
    uint8_t *strict_is_nvl;
    size_t strict_words;
    unsigned long long *ev; // counters (see main)''')
    marker='__device__ __forceinline__ void trace_tx'
    replace(ker,marker,MARK+marker)
    replace(ker,'    trace_tx(s, c.now, c.h, c.d, p);','    strict_link_busy(s, c.ob().link, c.now, tx);\n    trace_tx(s, c.now, c.h, c.d, p);')
    replace(ker,'    trace_tx(s, c.now, c.node, port, p);','    strict_link_busy(s, c.w[port].ob.link, c.now, tx);\n    trace_tx(s, c.now, c.node, port, p);')
    replace(ker,'''    __threadfence();
#ifdef RDMA_PROF
    PV_ADD(0, 1);''','''    __threadfence();
    ++s.strict_steps[0];
    s.strict_steps[1] += nsl;
#ifdef RDMA_PROF
    PV_ADD(0, 1);''')
    marker='    int pend_n = 2;'
    replace(main,marker,f'''    s.strict_words = (size_t({CAP_NS}) / D + 63) / 64;
    s.strict_steps = dev_alloc<unsigned long long>(3);
    s.strict_activity = dev_alloc<unsigned long long>(2 * s.strict_words);
    vector<uint8_t> strict_is_nvl(L);
    for (int l = 0; l < L; ++l) {{
        const int a = lsend[l], b = link_dst[l];
        strict_is_nvl[l] = (a >= H && si[a-H].nvs) || (b >= H && si[b-H].nvs);
    }}
    s.strict_is_nvl = dev_copy(strict_is_nvl);
    printf("strict_window requested=%d global=%d static_width=%d entity=%d rank_ahead=%d rank_enabled=%zu nvl_links=%zu\\n",
           kw, s.gwin, s.gk_static, s.any_win, s.rank_ahead, std::count(rk_ok.begin(),rk_ok.end(),1),
           std::count(strict_is_nvl.begin(), strict_is_nvl.end(), 1));
    for (int g = 0; g < H + S; ++g) {{
        if (g == H) printf("strict_window_switches\\n");
        printf("strict_width %d %u %u\\n", g<H ? 0 : 1, (unsigned)kb[g], (unsigned)kq[g]);
    }}
'''+marker)
    marker='    printf("flows done %u of %d; simulated'
    replace(main,marker,'''    {
        unsigned long long steps[3];
        CK(cudaMemcpy(steps, s.strict_steps, sizeof(steps), cudaMemcpyDeviceToHost));
        if (slot_end < 0 || (uint64_t)slot_end > s.strict_words * 64 || steps[2]) {
            fprintf(stderr, "strict activity cap exceeded: slot %lld late emissions %llu\\n", (long long)slot_end, steps[2]);
            return 1;
        }
        const size_t words = ((size_t)slot_end + 63) / 64;
        vector<unsigned long long> a(words), b(words);
        CK(cudaMemcpy(a.data(), s.strict_activity, words*sizeof(a[0]), cudaMemcpyDeviceToHost));
        CK(cudaMemcpy(b.data(), s.strict_activity+s.strict_words, words*sizeof(b[0]), cudaMemcpyDeviceToHost));
        unsigned long long nvl=0, fabric=0, both=0, all=0;
        for (size_t i=0; i<words; ++i) {
            const unsigned char rem = slot_end & 63;
            if (rem && i+1==words) {
                const unsigned long long mask = (1ull << rem) - 1;
                a[i] &= mask;
                b[i] &= mask;
            }
            nvl += __builtin_popcountll(a[i]);
            fabric += __builtin_popcountll(b[i]);
            both += __builtin_popcountll(a[i]&b[i]);
            all += __builtin_popcountll(a[i]|b[i]);
        }
        printf("strict_activity steps=%llu advanced_slots=%llu late=%llu observed_slots=%lld base_slot_ns=%lld "
               "nvl=%llu fabric=%llu both=%llu either=%llu\\n", steps[0], steps[1], steps[2],
               (long long)slot_end, (long long)delay, nvl, fabric, both, all);
    }
'''+marker)
    exe=out/'compiled/twins-ai'
    cmds=[['cmake','-S',tree,'-B',exe.parent,'-G','Ninja','-DTWINS_GPU_BACKEND=CUDA',
           '-DCMAKE_CUDA_ARCHITECTURES=120','-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc'],
          ['cmake','--build',exe.parent,'--target','twins-ai','-j4']]
    with (out/'build.log').open('wb') as stream:
        for cmd in cmds:
            subprocess.run(['taskset','-c','6,8',*cmd],stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=600)
    save(out/'build-provenance.json',{'binary_sha256':sha(exe),'source_hashes':{str(p.relative_to(tree)):sha(p) for p in (hdr,main,ker)},
                                    'commands':[list(map(str,c)) for c in cmds]})
    return exe


def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        binary,name,arm=Path(sys.argv[2]),sys.argv[3],sys.argv[4]
        row=sample(out,name,arm,binary)
        log=(out/'process.log').read_text()
        for k in ('strict_activity','strict_window'):
            match=re.findall(rf'^{k} (.*)$',log,re.M)
            row[k]={key:int(v) for key,v in re.findall(r'(\w+)=(\d+)',match[0])} if match else None
        hist={}
        for kind,b,q in re.findall(r'^strict_width (\d+) (\d+) (\d+)$',log,re.M):
            label=f'{kind}/{b}/{q}'
            hist[label]=hist.get(label,0)+1
        row['effective_width_histogram']=hist
        a=row['strict_activity']
        row['accepted']=row['accepted'] and bool(a) and a['late']==0 and a['nvl']+a['fabric']==a['both']+a['either']
        save(out/'receipt.json',row)
        print('AI counters',name,arm,row['accepted'],a,flush=True)
        return 0
    out.mkdir()
    binary=build(out/'build')
    for name in reversed(CASES):
        for arm in ARMS:
            subprocess.run(['flock',LOCK,sys.executable,OBSERVER,out/'resources'/name/arm,
                            sys.executable,__file__,out/name/arm,binary,name,arm],check=True)


if __name__=='__main__':
    main()
