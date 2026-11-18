"""Compile a distinct scratch executable against the archived author's libraries."""
import argparse,os,subprocess,shutil
from pathlib import Path
p=argparse.ArgumentParser()
for key in ('tree','frontend','out','libdir'):p.add_argument(key,type=Path)
p.add_argument('--simai-objects',type=Path)
p.add_argument('--simai-libs',type=Path)
a=p.parse_args();base=Path(__file__).resolve().parent;root=base.parents[2];tree=a.tree.resolve();out=a.out.resolve();out.mkdir(exist_ok=True,parents=True)
s=(tree/'scratch/network-load-balance.cc').read_text()
def edit(old,new):
    global s
    if s.count(old)!=1:raise ValueError(old)
    s=s.replace(old,new)
edit('uint64_t avg_nic_rate;', 'uint64_t avg_nic_rate = 0;')
# Frozen scale fixtures express integer nanoseconds. The legacy Seconds(double)
# conversion can truncate these exact decimal inputs a nanosecond early.
assert s.count('Seconds(flow_input.start_time)') == 2
s=s.replace('Seconds(flow_input.start_time)', 'NanoSeconds(std::llround(flow_input.start_time * 1e9))')
edit('if (dev) dev->TraceConnectWithoutContext("PhyTxBegin", MakeBoundCallback(&tx_begin, i, dev->GetIfIndex()));','''if (dev) {
                    bool keep = true;
                    if (const char *ports = getenv("TWINS_TRACE_PORTS")) {
                        static std::set<std::pair<unsigned,unsigned>> selected;
                        if (selected.empty()) {
                            std::ifstream in(ports);
                            unsigned node, port;
                            while (in >> node >> port) selected.insert(std::make_pair(node,port));
                            if (!in.eof() || selected.empty()) return 2;
                        }
                        keep = selected.count(std::make_pair(i,dev->GetIfIndex()));
                    }
                    if (keep) dev->TraceConnectWithoutContext("PhyTxBegin", MakeBoundCallback(&tx_begin, i, dev->GetIfIndex()));
                }''')
edit('static FILE *tx_out;', (base/'live.inc').read_text()+'\nstatic FILE *tx_out;')
edit('    Settings::cnt_finished_flows++;','''    if (live_fct) {
        fprintf(live_fct, "%d %lu %lu\\n", q->m_flow_id,q->startTime.GetTimeStep(),(Simulator::Now()-q->startTime).GetTimeStep());
        deliver(0,q->m_flow_id);
    }
    Settings::cnt_finished_flows++;''')
edit('    if (found_topo2bdpMap == false)', '''    if (const char* v=getenv("IRN_BDP_BYTES")) {
        irn_bdp_lookup=std::stoul(v);
        found_topo2bdpMap=irn_bdp_lookup>0;
    }
    if (found_topo2bdpMap == false)''')
edit('    Simulator::Schedule(Seconds(flowgen_start_time),\n                        &stop_simulation_middle);','    if (!getenv("AI_WORKLOAD")) Simulator::Schedule(Seconds(flowgen_start_time), &stop_simulation_middle);')
edit('    Simulator::Run();','''    if (getenv("AI_WORKLOAD")) {
        live_fct=fopen("messages.fct","w");
        if (!live_fct) return 2;
        for (uint32_t i=0;i<n.GetN();i++) for (uint32_t j=0;j<n.Get(i)->GetNDevices();j++) {
            Ptr<QbbNetDevice> dev=DynamicCast<QbbNetDevice>(n.Get(i)->GetDevice(j));
            if (dev) dev->TraceConnectWithoutContext("PhyTxBegin",MakeBoundCallback(&live_tx,i,j));
        }
        start_frontend();
    }
    Simulator::Run();
    if (live_fct) { fclose(live_fct); if(!save_frontend()) return 3; }''')
if a.simai_libs:
    s=s.replace('static FILE *live_fct;', 'extern "C" { extern unsigned long long live_irn_timeouts,live_irn_sacks,live_irn_retx; }\nstatic FILE *live_fct;')
    s=s.replace('fclose(live_fct); if(!save_frontend())', 'fclose(live_fct); fprintf(stderr,"live counters drops=%u sacks=%llu retransmitted=%llu timeouts=%llu\\n", Settings::dropped_pkt_sw_ingress+Settings::dropped_pkt_sw_egress,live_irn_sacks,live_irn_retx,live_irn_timeouts); if(!save_frontend())')
if a.simai_objects or a.simai_libs:
    s=s.replace('Seconds(nic->GetDataRate().CalculateTxTime(p->GetSize()))','NanoSeconds(uint64_t(p->GetSize()) * 8000000000ULL / nic->GetDataRate().GetBitRate())')
mod=out/'reference.cc';mod.write_text(s)
exe=out/'network-load-balance'
lib=tree/'build';runtime=a.libdir.resolve();linked=a.simai_libs.resolve() if a.simai_libs else lib
# Preserve the ns-3 build's preprocessor flags and old template dialect.
cmd=['g++','-O3','-std=gnu++11','-Wno-deprecated-declarations','-DHAVE_PACKET_H=1','-DHAVE_SQLITE3=1','-DHAVE_IF_TUN_H=1','-I'+str(lib),'-I'+str(tree),'-c',str(mod),'-o',str(out/'reference.o')]
subprocess.run(cmd,check=True)
subprocess.run(['g++','-O3','-std=c++17','-I'+str(root/'src'),str(base/'frontend_bridge.cc'),out/'reference.o',*(sorted(a.simai_objects.glob('*.o')) if a.simai_objects else []),a.frontend.resolve(),'-Wl,--no-as-needed',*sorted(linked.glob('libns3*.so')),'-pthread','-Wl,-rpath-link,'+str(runtime),'-Wl,-rpath,'+str(linked),'-o',exe],check=True)
