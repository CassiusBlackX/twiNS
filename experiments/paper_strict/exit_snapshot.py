"""Read original simulator queues and timer state at the native final boundary."""
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from crosspod import ROOT, LOCK, OBSERVER, replace

TWINS_HELPER = r'''
#ifndef TWINS_LINK_TIMING
static void report_exit_snapshot(const Sim &s, const std::vector<FlowRec> &flows)
{
    const int64_t last = ddown(s.snapshot_final_slot, 1)[0];
    const int64_t boundary = (last + 1) * SLOT_PS;
    auto sum = [](const std::vector<int> &v) {
        return std::accumulate(v.begin(), v.end(), 0LL);
    };
    const auto host = ddown(s.hs.q_size, s.H);
    const auto ports = ddown(s.ps.q_size, (size_t)s.S*s.k);
    const auto count = ddown(s.msg_cnt, (size_t)NB*s.L);
    uint64_t in_flight=0;
    for (size_t i=0; i<count.size(); ++i)
    {
        const int64_t tag=count[i] >> 16;
        if (tag < last-1 || tag > last || !(count[i]&65535))
            continue;
        for (const auto &frame : ddown(s.msg+i*MAX_TX, count[i]&65535))
            in_flight += frame.ts >= boundary;
    }
    const auto ns = ddown(s.hs.n_snd, s.H), nr = ddown(s.hs.n_rcv, s.H);
    const auto so = ddown(s.hs.snd_offset, s.H+1), ro = ddown(s.hs.rcv_offset, s.H+1);
    const auto send = ddown(s.hs.act_snd, so.back()), recv = ddown(s.hs.act_rcv, ro.back());
    const auto next = ddown(s.hs.pend_next, s.H), end = ddown(s.hs.pend_end, s.H);
    uint64_t pending=0, rto=0, syn=0, ack=0, synack=0, lastack=0, finack=0;
    for (int h=0;h<s.H;++h)
    {
        pending+=end[h]-next[h];
        for (int i=0;i<ns[h];++i)
        {
            const auto &f=flows[send[so[h]+i]];
            rto+=f.rto_at>=0;
            syn+=f.syn_at>=0;
        }
        for (int i=0;i<nr[h];++i)
        {
            const auto &f=flows[recv[ro[h]+i]];
            ack+=f.delack_at>=0;
            synack+=f.srv_syn_at>=0;
            lastack+=f.r_last_at>=0;
            finack+=f.r_frx_at>=0;
        }
    }
    std::cout << "strict_exit last_slot=" << last
        << " horizon_ps=" << boundary << " host_queued=" << sum(host)
        << " switch_queued=" << sum(ports) << " link_in_flight=" << in_flight
        << " future_messages=" << pending << " sender_rto=" << rto
        << " sender_syn_or_send=" << syn << " delayed_ack=" << ack
        << " receiver_synack=" << synack << " receiver_lastack=" << lastack
        << " receiver_finack=" << finack << "\n";
}
#endif
'''

GEDES_HELPER=r'''
  void Topology::ReportExitSnapshot()
  {
    std::set<const void*> seen;
    auto count = [&seen](const auto& queues) {
      unsigned long long items=0;
      for (auto ptr : queues)
      {
        if (!ptr || !seen.insert(ptr).second) continue;
        using Queue=std::remove_pointer_t<decltype(ptr)>;
        Queue c{};
        if (cudaMemcpy(&c,ptr,sizeof(c),cudaMemcpyDeviceToHost)!=cudaSuccess)
          throw std::runtime_error("exit queue copy failed");
        items+=c.size;
      }
      return items;
    };
    const auto wireIn=count(m_ch_frame_ingress_queues);
    const auto wireOut=count(m_ch_frame_egress_queues);
    const auto switchItems=count(m_sw_frame_ingress_queues)+count(m_sw_frame_egress_queues);
    const auto nodeFrames=count(m_node_frame_ingress_queues)+count(m_node_frame_egress_queues);
    const auto ipv4Items=count(m_node_ipv4_ingress_queues)+count(m_node_ipv4_egress_queues)
        +count(m_node_ipv4_local_delivery_queues);
    const auto ipv4Errors=count(m_node_ipv4_error_queues);
    const auto tcpItems=count(m_node_tcp_ingress_queues)+count(m_node_tcp_egress_queues);
    const auto cacheFrames=count(m_cache_frame_queues);
    unsigned long long active_rto=0, arrivals=0, bytes_left=0, future_messages=0, received_segments=0;
    for (auto ptr : m_tcp_connections)
    {
      if (!ptr) continue;
      TCPConnection con{};
      if (cudaMemcpy(&con,ptr,sizeof(con),cudaMemcpyDeviceToHost)!=cudaSuccess)
        throw std::runtime_error("exit TCP copy failed");
      active_rto+=con.una>con.snd;
      arrivals+=con.unacked_packets;
      received_segments+=con.record_num;
      if (con.planned_bytes>con.acked_bytes)
        bytes_left+=con.planned_bytes-con.acked_bytes;
      if (con.flows)
      {
        GPUQueue<Flow> q{};
        if (cudaMemcpy(&q,con.flows,sizeof(q),cudaMemcpyDeviceToHost)!=cudaSuccess)
          throw std::runtime_error("exit application queue copy failed");
        future_messages+=q.size;
      }
    }
    std::cout << "strict_exit native_finished=" << IsFinished() << " channel_source_queued=" << wireIn
      << " channel_arrival_queued=" << wireOut << " switch_frame_queued=" << switchItems
      << " node_frame_queued=" << nodeFrames << " ipv4_queued=" << ipv4Items
      << " ipv4_error_queued=" << ipv4Errors << " tcp_queued=" << tcpItems
      << " cache_frames=" << cacheFrames << " rto_with_outstanding_data=" << active_rto
      << " receiver_unacked_packets=" << arrivals << " receiver_ooo_ranges=" << received_segments
      << " admitted_payload_not_acked=" << bytes_left << " future_messages=" << future_messages << "\n";
  }
'''

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def sources(out):
    original=ROOT/'crosspod/20261122T205340Z/results/builds'
    for name in ('twins','GeDES'):
        shutil.copytree(original/name,out/name,ignore=shutil.ignore_patterns('cmake','build'))
    twins=out/'twins/src'
    replace(twins/'model.h','    int *finished, *finished_host;',
            '    int *finished, *finished_host;\n    int64_t *snapshot_final_slot;')
    replace(twins/'kernel.cuh','                *s.finished = 1;',
            '                *s.finished = 1;\n                *s.snapshot_final_slot = t;')
    main=twins/'main.cu'
    replace(main,'#include <numeric>', '#include <numeric>\n#include <iostream>')
    replace(main,'int main(int argc, char **argv)',TWINS_HELPER+'\nint main(int argc, char **argv)')
    replace(main,'    s.finished = dalloc<int>(1);',
            '    s.finished = dalloc<int>(1);\n    s.snapshot_final_slot = dalloc<int64_t>(1);')
    replace(main,'    if (fd < (unsigned)M)',
            '#ifndef TWINS_LINK_TIMING\n    report_exit_snapshot(s, out_rec);\n#endif\n    if (fd < (unsigned)M)')
    ge=out/'GeDES'
    replace(ge/'main.cc','    topology.RecordFlowResults(output_file_name);',
            '    topology.ReportExitSnapshot();\n    topology.RecordFlowResults(output_file_name);')
    headers=ge/'code/c++/include'
    replace(headers/'topology.h','    void RecordFlowResults(std::string file_name);',
            '    void RecordFlowResults(std::string file_name);\n    void ReportExitSnapshot();')
    cpp=ge/'code/c++/src/components/topology.cc'
    replace(cpp,'#include "topology.h"', '#include "topology.h"\n#include <set>\n#include <stdexcept>\n#include <type_traits>')
    replace(cpp,'  bool Topology::IsFinished()\n  {',GEDES_HELPER+'\n  bool Topology::IsFinished()\n  {')
    return original

def compile(output):
    result=output/'build-info'
    result.mkdir()
    build_tree=output.parent/'builds'
    original=sources(build_tree)
    envelopes=[('twins','twins',['-DTWINS_GPU_BACKEND=CUDA']),
               ('GeDES','GeDES',['-DGEDES_GPU_BACKEND=CUDA','-DCMAKE_CUDA_FLAGS=-DENABLE_DCTCP=0'])]
    for name,target,opts in envelopes:
        path=build_tree/name
        changed=[p for p in path.rglob('*') if p.is_file()
                 and (original/name/p.relative_to(path)).is_file()
                 and sha(p)!=sha(original/name/p.relative_to(path))]
        (result/f'{name}.json').write_text(json.dumps({str(p.relative_to(path)):sha(p) for p in changed},indent=2)+'\n')
        with (result/f'{name}.log').open('wb') as stream:
            subprocess.run(['cmake','-S',path,'-B',path/'cmake','-G','Ninja','-DCMAKE_CUDA_ARCHITECTURES=120',
                '-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc',*opts],stdout=stream,stderr=subprocess.STDOUT,check=True)
            subprocess.run(['cmake','--build',path/'cmake','--target',target,'-j2'],stdout=stream,stderr=subprocess.STDOUT,check=True)
    return build_tree

def observe(out,build):
    import crosspod
    regular=ROOT/'paced_shared/20261122T211357Z/results/measurements'
    old=ROOT/'crosspod/20261122T205340Z/results/measurements'
    engines={'twins':build/'twins/cmake/twins','gedes':build/'GeDES/cmake/GeDES'}
    out.mkdir()
    values=[]
    for case in [*sorted(regular.glob('k*')), *sorted(old.glob('k*-overlap'))]:
        condition='paced' if case.parent==regular else 'burst'
        spec=json.loads((case/'input.json').read_text())
        work=out/case.name
        work.mkdir()
        crosspod.route_hashes(case,work)
        for engine in engines:
            trial=crosspod.run(work,engines,case,spec,engine,0)
            actual=work/f'trial-0/{engine}'
            log=(actual/'process.log').read_text()
            snapshot=next((line for line in log.splitlines() if line.startswith('strict_exit ')),None)
            if not snapshot:
                raise RuntimeError(f'no snapshot: {case.name} {engine}')
            fields={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',snapshot)}
            baseline=case/f'trial-0/{engine}/fct.csv'
            equivalent=sha(actual/'fct.csv')==sha(baseline)
            row=dict(case=case.name,regime=condition,engine=engine,fct_equal=equivalent,
                snapshot=fields,native_result=trial)
            values.append(row)
            print(case.name,engine,equivalent,fields,flush=True)
        (out/'summary.json').write_text(json.dumps(values,indent=2)+'\n')
    if not all(row['fct_equal'] for row in values):
        return 1
    return 0

def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        return observe(out,Path(sys.argv[2]))
    out.mkdir(parents=True)
    build=compile(out)
    return subprocess.run(['flock',LOCK,sys.executable,OBSERVER,out/'resources',
        sys.executable,__file__,out/'measurements',build]).returncode

if __name__=='__main__':
    raise SystemExit(main())
