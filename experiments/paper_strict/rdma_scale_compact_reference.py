"""Keep HPCC's reference handlers and ECMP order while streaming route initialization."""

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import monotonic_ns

from crosspod import ROOT,replace,sha
from pfc_sustained import save
from rdma_scale_reference import DATA,MODEL,HOST_LOCK,TRACES_UNTIL


ROUTES='''// The original traversal, in its ById neighbour order. Per-destination metrics are
// reduced immediately, and route entries are installed in the original order for each node.
static void BuildRoutesCompact(uint32_t hosts, uint32_t nodes)
{
    struct Edge { uint32_t nbr, reverse_port; uint64_t delay, bw; };
    vector<vector<Edge>> adjacency(nodes);
    for (const auto &row : nbr2if)
        for (const auto &entry : row.second)
            if (entry.second.up)
                adjacency[row.first->GetId()].push_back(Edge{entry.first->GetId(),
                    nbr2if[entry.first][row.first].idx, entry.second.delay, entry.second.bw});
    // A destination may receive either data or feedback. Other destinations never enter the model.
    vector<vector<uint32_t>> host_peers(hosts);
    for (const auto &f : flows) {
        host_peers.at(f.src).push_back(f.dst);
        host_peers.at(f.dst).push_back(f.src);
    }
    for (auto &peers : host_peers) std::sort(peers.begin(),peers.end());
    vector<int> dis(nodes);
    vector<uint64_t> delay(nodes), txdelay(nodes), bw(nodes);
    vector<uint32_t> q;
    maxRtt=maxBdp=0;
    for (uint32_t host=0; host<hosts; ++host)
    {
        fill(dis.begin(),dis.end(),-1);
        q.clear();q.push_back(host);
        dis[host]=0;delay[host]=txdelay[host]=0;bw[host]=~0ull;
        auto ip=node_id_to_ip(host);
        for (size_t i=0;i<q.size();++i)
        {
            const uint32_t now=q[i];
            const int d=dis[now];
            for (const auto &link : adjacency[now])
            {
                const uint32_t next=link.nbr;
                if (dis[next]<0)
                {
                    dis[next]=d+1;
                    delay[next]=delay[now]+link.delay;
                    txdelay[next]=txdelay[now]+packet_payload_size*1000000000lu*8/link.bw;
                    bw[next]=min(bw[now],link.bw);
                    if (next>=hosts) q.push_back(next);
                }
                if (dis[next]==d+1 && !host_peers[host].empty())
                {
                    if (next>=hosts)
                        DynamicCast<SwitchNode>(n.Get(next))->AddTableEntry(ip,link.reverse_port);
                    else if (std::binary_search(host_peers[next].begin(),host_peers[next].end(),host))
                        n.Get(next)->GetObject<RdmaDriver>()->m_rdma->AddTableEntry(ip,link.reverse_port);
                }
            }
        }
        for (uint32_t src=0;src<hosts;++src)
        {
            if (dis[src]<0) std::abort();
            const uint64_t rtt=delay[src]*2+txdelay[src];
            maxRtt=max(maxRtt,rtt);
            maxBdp=max(maxBdp,rtt*bw[src]/1000000000/8);
        }
    }
}

'''


def build(out):
    out.mkdir()
    src=out/'rdma-ref-compact.cc'
    old=MODEL/'simulation/scratch/rdma-ref.cc'
    s=old.read_text()
    begin=s.index('static void CalculateRoute(')
    end=s.index('static Ipv4Address node_id_to_ip',begin)
    s=s[:begin]+s[end:]
    start=s.index('    // flows\n    ifstream ff(flow_file.c_str());')
    end=s.index('    for (uint32_t i = 0; i < H; i++)',start)
    flows=s[start:end]
    s=s[:start]+s[end:]
    start=s.index('    for (uint32_t i = 0; i < H; i++)\n        CalculateRoute(n.Get(i));')
    end=s.index('    printf("maxRtt=',start)
    s=s[:start]+flows+'''    if (global_t != 1 || getenv("DUMP_ROUTES")) return 2;
    const auto strict_start=std::chrono::steady_clock::now();
    BuildRoutesCompact(H,node_num);
    printf("strict_build_ns=%lld\\n",(long long)std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now()-strict_start).count());
'''+s[end:]
    start=s.index('    if (getenv("DUMP_ROUTES"))\n    {')
    end=s.index('    for (uint32_t i = 0; i < H; i++)\n        for',start)
    s=s[:start]+s[end:]
    src.write_text(s)
    replace(src,'#include <algorithm>','#include <algorithm>\n#include <chrono>')
    replace(src,'static void ScheduleFlowInputs()',ROUTES+'static void ScheduleFlowInputs()')
    replace(src,'    for (uint32_t i = 0; i < H; i++)\n        for (uint32_t j = 0; j < H; j++)\n            portNumber[i][j] = 10000;',
                '    for (const auto &f : flows) portNumber[f.src][f.dst] = 10000;')
    replace(src,'    Simulator::Run();','''    const auto strict_sim=std::chrono::steady_clock::now();
    Simulator::Run();
    printf("strict_event_loop_ns=%lld\\n",(long long)std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now()-strict_sim).count());''')
    cmd=['docker','run','--rm','--network=none','--cpuset-cpus','10','--memory=6g','--user',f'{os.getuid()}:{os.getgid()}',
         '-v',f'{MODEL}:/hpcc:ro','-v',f'{out}:/result','--entrypoint','/bin/bash','localhost/hpcc-build','-c',
         'g++ -O3 -DNDEBUG -std=c++11 -I/hpcc/simulation/build /result/rdma-ref-compact.cc '
         '-Wl,--no-as-needed /hpcc/simulation/build/libns3.*-optimized.so -Wl,--as-needed -lpthread -lrt -o /result/rdma-ref-compact']
    with (out/'build.log').open('wb') as stream:
        subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT,timeout=220,check=True)
    save(out/'build-provenance.json',{'command':cmd,'original_sha256':sha(old),'modified_sha256':sha(src),
                                    'binary_sha256':sha(out/'rdma-ref-compact')})


def run(root,case,kind,old_ref=None,core=4,memory='70g'):
    dest=root/kind
    dest.mkdir(parents=True)
    mark=f'twins-compact-hpcc-{os.getpid()}-{kind}'
    command=['docker','run','--rm','--name',mark,'--network=none','--ulimit','core=0','--cpuset-cpus',str(core),
             f'--memory={memory}',f'--memory-swap={memory}','--user',f'{os.getuid()}:{os.getgid()}',
             '-v',f'{MODEL}:/hpcc:ro','-v',f'{BUILD}:/driver:ro','-v',f'{case}:/case:ro','-v',f'{dest}:/result','-w','/result',
             '-e','LD_LIBRARY_PATH=/hpcc/simulation/build','--entrypoint','/bin/bash','localhost/hpcc-build','-c',
             '"$@"; rc=$?; printf "cgroup_memory_peak_bytes="; cat /sys/fs/cgroup/memory.peak; exit "$rc"','_',
             '/driver/rdma-ref-compact','/case/config.log','--topo=/case/topo.log',f'--flows=/case/{kind}.csv',
             '--out=/result/output.fct','--txtrace=/result/wire.raw',f'--until={TRACES_UNTIL}','CC_MODE=1']
    save(dest/'started.json',{'command':command})
    timer=monotonic_ns()
    with (dest/'process.log').open('wb') as log:
        try:
            rc=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=900).returncode
        except subprocess.TimeoutExpired:
            subprocess.run(['docker','stop','-t','5',mark],stdout=log,stderr=subprocess.STDOUT,timeout=20)
            rc=124
    elapsed=monotonic_ns()-timer
    proto=json.loads((case/'input.json').read_text())
    log=(dest/'process.log').read_text()
    raw=dest/'wire.raw'
    detail=dict(traffic=kind,wall_ns=elapsed,rc=rc,k=proto['k'],command=command,
                metrics={k:int(v) for k,v in re.findall(r'(strict_build_ns|strict_event_loop_ns|cgroup_memory_peak_bytes|maxRtt|maxBdp)=(\d+)',log)})
    if raw.is_file():
        ports={tuple(map(str,p)) for p in proto['selected_ports']}
        with raw.open() as stream,(dest/'selected-wire.log').open('w') as out:
            for row in stream:
                x=row.split()
                if len(x)==10 and tuple(x[1:3]) in ports:
                    out.write(row)
        detail['full_trace_sha256']=sha(raw)
    f=dest/'output.fct'
    if f.is_file():
        detail['flows']=len(f.read_bytes().splitlines())
    if old_ref:
        detail['stock_fct_equal']=f.is_file() and sorted(f.read_bytes().splitlines())==sorted(old_ref.read_bytes().splitlines())
    save(dest/'receipt.json',detail)
    print('compact HPCC',root.name,kind,rc,detail.get('flows'),elapsed/1e9,detail['metrics'],flush=True)
    return detail


def main():
    global BUILD
    root=Path(sys.argv[1]).resolve()
    root.mkdir()
    BUILD=root/'build'
    build(BUILD)
    wanted=root/'frozen-k32'
    wanted.mkdir()
    for src,dst in (('ft32.topo','topo.log'),('hpcc.cfg','config.log'),('k32_a2a30.csv','distributed.csv'),('k32_incast16.csv','incast.csv')):
        shutil.copy2(DATA/src,wanted/dst)
    save(wanted/'input.json',{'k':32,'selected_ports':[[8192,1],[8192,17]]})
    prior=next(p for p in sorted((ROOT/'rdma_scale_reference').glob('20*'),reverse=True) if (p/'results/inputs/k40/input.json').is_file())
    rows=[]
    for kind,origin in (('incast','incast16'),('distributed','a2a30')):
        with HOST_LOCK.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            v=run(root/'checking-k32',wanted,kind,DATA/f'k32_{origin}_cc1.fct')
        rows.append(v);save(root/'summary.json',rows)
        assert v['rc']==0 and v['stock_fct_equal']
    for kind in ('incast','distributed'):
        with HOST_LOCK.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            v=run(root/'reference-k40',prior/'results/inputs/k40',kind)
        rows.append(v);save(root/'summary.json',rows)


if __name__=='__main__':
    main()
