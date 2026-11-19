extern "C" void start_frontend();
extern "C" bool advance_frontend(int,int,long long);
extern "C" bool save_frontend();
static std::map<std::pair<unsigned,unsigned>,unsigned> pair_ports;
static std::map<std::tuple<unsigned,unsigned,unsigned>,int> waiting_send;
static FILE *live_fct;
static void deliver(int kind,int id) {
    if(advance_frontend(kind,id,Simulator::Now().GetNanoSeconds())) Simulator::Stop(NanoSeconds(1));
}
extern "C" void enqueue_timer(int id,long long delay) {
    Simulator::Schedule(NanoSeconds(delay),&deliver,2,id);
}
static void open_flow(int id,int src,int dst,unsigned long long size) {
    auto &next=pair_ports[std::make_pair(src,dst)];
    unsigned sport=10000+next++;
    waiting_send[std::make_tuple(src,dst,sport)]=id;
    n.Get(src)->GetObject<RdmaDriver>()->AddQueuePair(size,3,serverAddress[src],serverAddress[dst],sport,100,
        has_win ? (global_t==1 ? maxBdp : pairBdp[n.Get(src)][n.Get(dst)]) : 0,
        global_t==1 ? maxRtt : pairRtt[n.Get(src)][n.Get(dst)],id);
}
extern "C" void enqueue_flow(int id,int src,int dst,unsigned long long size) {
    Simulator::Schedule(NanoSeconds(3000),&open_flow,id,src,dst,size);
}
static void live_tx(unsigned node,unsigned dev,Ptr<const Packet> p) {
    // SimAI schedules SendPacketComplete only for frames in this size range.
    if(p->GetSize()<=60 || p->GetSize()>=9000) return;
    CustomHeader ch(CustomHeader::L2_Header | CustomHeader::L3_Header | CustomHeader::L4_Header);
    ch.getInt=1;
    p->PeekHeader(ch);
    if(ch.l3Prot!=0x11 || node!=Settings::ip_to_node_id(Ipv4Address(ch.sip))) return;
    auto it=waiting_send.find(std::make_tuple(node,Settings::ip_to_node_id(Ipv4Address(ch.dip)),ch.udp.sport));
    if(it==waiting_send.end()) return;
    Ptr<QbbNetDevice> nic=DynamicCast<QbbNetDevice>(n.Get(node)->GetDevice(dev));
    Time tx=Seconds(nic->GetDataRate().CalculateTxTime(p->GetSize()));
    Simulator::Schedule(tx,&deliver,1,it->second);
    waiting_send.erase(it);
}
