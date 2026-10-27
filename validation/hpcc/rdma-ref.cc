// rdma-ref: the reference scenario for TwiNS's RDMA model. It is the HPCC simulator's scenario
// (simulation/scratch/third.cc of github.com/alibaba-edu/High-Precision-Congestion-Control,
// ns-3.17) on TwiNS's topology and flow files, with three changes that make it reproducible:
//   - the topology maps are ordered by node id, not by Ptr<Node> (a heap address), so the
//     equal-cost next hops of a switch are in a defined order (by the neighbour's id);
//   - each switch egress port draws its ECN marks from its own random stream (switch-mmu.patch);
//   - flows start at integer nanoseconds.
// Everything else (QbbNetDevice, RdmaHw, SwitchNode, SwitchMmu) is the HPCC code as it is.
//
// usage: rdma-ref CONFIG --topo=F --flows=F --out=F [--txtrace=F --from=ns --until=ns]
//                        [--rate=100Gbps --delay=1000ns] [KEY=VALUE ...]
// CONFIG is an HPCC config file (mix/config.txt); KEY=VALUE overrides one of its keys. TOPOLOGY_FILE,
// FLOW_FILE and the trace and monitor keys are ignored.
//   topology (workloads/topo.py): "hosts H", "switches S", "link A B" (A, B: h<i> or s<j>);
//     node ids: hosts 0..H-1, then switches H..H+S-1; links installed in file order
//   flows: CSV src,dst,flow_size,start_ns[,...] (hosts); flow i is the i-th row
//   out: one line per flow, "id start_ns fct_ns", in completion order
//   txtrace: one line per transmission start in [from, until]: ns node dev kind src dst sport
//     seq bytes ecn (kind D data, A ACK, N NACK, P pause, R resume; for P/R src is the queue)

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>
#include "ns3/core-module.h"
#include "ns3/qbb-helper.h"
#include "ns3/point-to-point-helper.h"
#include "ns3/applications-module.h"
#include "ns3/internet-module.h"
#include "ns3/packet.h"
#include "ns3/error-model.h"
#include <ns3/rdma.h>
#include <ns3/rdma-client.h>
#include <ns3/rdma-client-helper.h>
#include <ns3/rdma-driver.h>
#include <ns3/switch-node.h>

using namespace ns3;
using namespace std;

// ---------------------------------------------------------------- configuration
static map<string, vector<string> > cfg;

static void cfg_read(const string &path)
{
    ifstream f(path.c_str());
    if (!f)
    {
        fprintf(stderr, "cannot open %s\n", path.c_str());
        exit(1);
    }
    string line;
    while (getline(f, line))
    {
        istringstream is(line);
        string key, v;
        if (!(is >> key))
            continue;
        vector<string> vals;
        while (is >> v)
            vals.push_back(v);
        cfg[key] = vals;
    }
}
static bool has(const char *k) { return cfg.count(k) && !cfg[k].empty(); }
static string str(const char *k, const string &d) { return has(k) ? cfg[k][0] : d; }
static double num(const char *k, double d) { return has(k) ? atof(cfg[k][0].c_str()) : d; }
static uint32_t u32(const char *k, uint32_t d) { return has(k) ? (uint32_t)strtoul(cfg[k][0].c_str(), 0, 10) : d; }

// ---------------------------------------------------------------- topology and routes
struct ById
{
    bool operator()(const Ptr<Node> &a, const Ptr<Node> &b) const { return a->GetId() < b->GetId(); }
};
struct Interface
{
    uint32_t idx;
    bool up;
    uint64_t delay;
    uint64_t bw;
    Interface() : idx(0), up(false), delay(0), bw(0) {}
};
static NodeContainer n;
static map<Ptr<Node>, map<Ptr<Node>, Interface, ById>, ById> nbr2if;
static map<Ptr<Node>, map<Ptr<Node>, vector<Ptr<Node> >, ById>, ById> nextHop;
static map<Ptr<Node>, map<Ptr<Node>, uint64_t, ById>, ById> pairDelay, pairTxDelay;
static map<uint32_t, map<uint32_t, uint64_t> > pairBw, pairRtt;
static map<Ptr<Node>, map<Ptr<Node>, uint64_t, ById>, ById> pairBdp;
static uint32_t packet_payload_size = 1000;
static uint64_t maxRtt, maxBdp;

static void CalculateRoute(Ptr<Node> host)
{
    vector<Ptr<Node> > q;
    map<Ptr<Node>, int, ById> dis;
    map<Ptr<Node>, uint64_t, ById> delay, txDelay, bw;
    q.push_back(host);
    dis[host] = 0;
    delay[host] = 0;
    txDelay[host] = 0;
    bw[host] = 0xfffffffffffffffflu;
    for (int i = 0; i < (int)q.size(); i++)
    {
        Ptr<Node> now = q[i];
        int d = dis[now];
        for (auto it = nbr2if[now].begin(); it != nbr2if[now].end(); it++)
        {
            if (!it->second.up)
                continue;
            Ptr<Node> next = it->first;
            if (dis.find(next) == dis.end())
            {
                dis[next] = d + 1;
                delay[next] = delay[now] + it->second.delay;
                txDelay[next] = txDelay[now] + packet_payload_size * 1000000000lu * 8 / it->second.bw;
                bw[next] = std::min(bw[now], it->second.bw);
                if (next->GetNodeType() == 1)
                    q.push_back(next);
            }
            if (d + 1 == dis[next])
                nextHop[next][host].push_back(now);
        }
    }
    for (auto it : delay)
        pairDelay[it.first][host] = it.second;
    for (auto it : txDelay)
        pairTxDelay[it.first][host] = it.second;
    for (auto it : bw)
        pairBw[it.first->GetId()][host->GetId()] = it.second;
}

static void SetRoutingEntries()
{
    for (auto i = nextHop.begin(); i != nextHop.end(); i++)
    {
        Ptr<Node> node = i->first;
        for (auto j = i->second.begin(); j != i->second.end(); j++)
        {
            Ipv4Address dstAddr = j->first->GetObject<Ipv4>()->GetAddress(1, 0).GetLocal();
            for (Ptr<Node> next : j->second)
            {
                uint32_t interface = nbr2if[node][next].idx;
                if (node->GetNodeType() == 1)
                    DynamicCast<SwitchNode>(node)->AddTableEntry(dstAddr, interface);
                else
                    node->GetObject<RdmaDriver>()->m_rdma->AddTableEntry(dstAddr, interface);
            }
        }
    }
}

static Ipv4Address node_id_to_ip(uint32_t id)
{
    return Ipv4Address(0x0b000001 + ((id / 256) * 0x00010000) + ((id % 256) * 0x00000100));
}
static uint32_t ip_to_node_id(Ipv4Address ip) { return (ip.Get() >> 8) & 0xffff; }
static uint32_t ip_to_node_id(uint32_t ip) { return (ip >> 8) & 0xffff; }

// ---------------------------------------------------------------- flows
struct Flow
{
    uint32_t src, dst;
    uint64_t size, start;
    uint32_t id;
};
static vector<Flow> flows; // sorted by start (stable)
static size_t next_flow;
static unordered_map<uint32_t, unordered_map<uint32_t, uint16_t> > portNumber;
static map<uint64_t, uint32_t> flow_of; // (src, dst, sport) -> id
static uint32_t has_win = 1, global_t = 1, flows_done;
static FILE *fct_out;

static uint64_t flow_key(uint32_t s, uint32_t d, uint32_t sport) { return ((uint64_t)s << 40) | ((uint64_t)d << 16) | sport; }

static void ScheduleFlowInputs()
{
    while (next_flow < flows.size() && NanoSeconds(flows[next_flow].start) == Simulator::Now())
    {
        const Flow &f = flows[next_flow];
        uint16_t port = portNumber[f.src][f.dst]++;
        flow_of[flow_key(f.src, f.dst, port)] = f.id;
        Ptr<Node> s = n.Get(f.src), d = n.Get(f.dst);
        RdmaClientHelper clientHelper(3, node_id_to_ip(f.src), node_id_to_ip(f.dst), port, 100, f.size,
                                      has_win ? (global_t == 1 ? maxBdp : pairBdp[s][d]) : 0,
                                      global_t == 1 ? maxRtt : pairRtt[f.src][f.dst]);
        ApplicationContainer appCon = clientHelper.Install(s);
        appCon.Start(Time(0));
        next_flow++;
    }
    if (next_flow < flows.size())
        Simulator::Schedule(NanoSeconds(flows[next_flow].start) - Simulator::Now(), ScheduleFlowInputs);
}

static void qp_finish(FILE *fout, Ptr<RdmaQueuePair> q)
{
    uint32_t sid = ip_to_node_id(q->sip), did = ip_to_node_id(q->dip);
    fprintf(fout, "%u %lu %lu\n", flow_of[flow_key(sid, did, q->sport)], q->startTime.GetTimeStep(),
            (Simulator::Now() - q->startTime).GetTimeStep());
    Ptr<RdmaDriver> rdma = n.Get(did)->GetObject<RdmaDriver>();
    rdma->m_rdma->DeleteRxQp(q->sip.Get(), q->m_pg, q->sport);
    if (++flows_done == flows.size())
        Simulator::Stop();
}

// ---------------------------------------------------------------- transmission trace
static FILE *tx_out;
static uint64_t tx_from = 0, tx_until = 0;

static void tx_begin(uint32_t node, uint32_t dev, Ptr<const Packet> p)
{
    uint64_t now = Simulator::Now().GetTimeStep();
    if (now < tx_from || now > tx_until)
        return;
    CustomHeader ch(CustomHeader::L2_Header | CustomHeader::L3_Header | CustomHeader::L4_Header);
    ch.getInt = 1;
    p->PeekHeader(ch);
    char kind = '?';
    uint32_t a = ip_to_node_id(ch.sip), b = ip_to_node_id(ch.dip), sport = 0, seq = 0;
    if (ch.l3Prot == 0x11)
        kind = 'D', sport = ch.udp.sport, seq = ch.udp.seq;
    else if (ch.l3Prot == 0xFC || ch.l3Prot == 0xFD)
        kind = ch.l3Prot == 0xFC ? 'A' : 'N', sport = ch.ack.dport, seq = ch.ack.seq;
    else if (ch.l3Prot == 0xFE)
        kind = ch.pfc.time > 0 ? 'P' : 'R', a = ch.pfc.qIndex, b = 0;
    fprintf(tx_out, "%lu %u %u %c %u %u %u %u %u %u\n", now, node, dev, kind, a, b, sport, seq, p->GetSize(),
            (unsigned)(ch.m_tos & 3));
}

// ---------------------------------------------------------------- main
int main(int argc, char *argv[])
{
    if (argc < 2)
    {
        fprintf(stderr, "usage: rdma-ref CONFIG --topo=F --flows=F --out=F [--txtrace=F --from=ns --until=ns] [KEY=VALUE ...]\n");
        return 1;
    }
    cfg_read(argv[1]);
    string topo_file, flow_file, out_file = "fct.txt", tx_file, rate = "100Gbps", delay = "1000ns";
    for (int i = 2; i < argc; i++)
    {
        string a = argv[i];
        size_t eq = a.find('=');
        string k = a.substr(0, eq), v = eq == string::npos ? "" : a.substr(eq + 1);
        if (k == "--topo")
            topo_file = v;
        else if (k == "--flows")
            flow_file = v;
        else if (k == "--out")
            out_file = v;
        else if (k == "--txtrace")
            tx_file = v;
        else if (k == "--from")
            tx_from = strtoull(v.c_str(), 0, 10);
        else if (k == "--until")
            tx_until = strtoull(v.c_str(), 0, 10);
        else if (k == "--rate")
            rate = v;
        else if (k == "--delay")
            delay = v;
        else if (k.compare(0, 2, "--") != 0 && eq != string::npos)
        {
            // KEY=VALUE, or KEY=V1,V2,... for a key with several values (KMAX_MAP=1,100000000000,800)
            vector<string> vals;
            size_t b = 0;
            for (size_t c; (c = v.find(',', b)) != string::npos; b = c + 1)
                vals.push_back(v.substr(b, c - b));
            vals.push_back(v.substr(b));
            cfg[k] = vals;
        }
        else
        {
            fprintf(stderr, "unknown argument %s\n", argv[i]);
            return 1;
        }
    }

    uint32_t cc_mode = u32("CC_MODE", 1);
    bool enable_qcn = u32("ENABLE_QCN", 1), dynamicth = u32("USE_DYNAMIC_PFC_THRESHOLD", 1);
    packet_payload_size = u32("PACKET_PAYLOAD_SIZE", 1000);
    uint32_t l2_chunk_size = u32("L2_CHUNK_SIZE", 0), l2_ack_interval = u32("L2_ACK_INTERVAL", 0);
    bool l2_back_to_zero = u32("L2_BACK_TO_ZERO", 0);
    double pause_time = num("PAUSE_TIME", 5);
    double alpha_resume_interval = num("ALPHA_RESUME_INTERVAL", 55), rp_timer = num("RP_TIMER", 1500);
    double ewma_gain = num("EWMA_GAIN", 1.0 / 16), rate_decrease_interval = num("RATE_DECREASE_INTERVAL", 4);
    uint32_t fast_recovery_times = u32("FAST_RECOVERY_TIMES", 5);
    string rate_ai = str("RATE_AI", "5Mb/s"), rate_hai = str("RATE_HAI", "50Mb/s"), min_rate = str("MIN_RATE", "100Mb/s");
    string dctcp_rate_ai = str("DCTCP_RATE_AI", "1000Mb/s");
    bool clamp_target_rate = u32("CLAMP_TARGET_RATE", 0);
    has_win = u32("HAS_WIN", 1);
    global_t = u32("GLOBAL_T", 1);
    uint32_t mi_thresh = u32("MI_THRESH", 5);
    bool var_win = u32("VAR_WIN", 0), fast_react = u32("FAST_REACT", 1), multi_rate = u32("MULTI_RATE", 1);
    bool sample_feedback = u32("SAMPLE_FEEDBACK", 0), rate_bound = u32("RATE_BOUND", 1);
    double u_target = num("U_TARGET", 0.95);
    uint32_t int_multi = u32("INT_MULTI", 1), ack_high_prio = u32("ACK_HIGH_PRIO", 0);
    uint32_t buffer_size = u32("BUFFER_SIZE", 16);
    unordered_map<uint64_t, uint32_t> rate2kmax, rate2kmin;
    unordered_map<uint64_t, double> rate2pmax;
    const char *maps[3] = {"KMAX_MAP", "KMIN_MAP", "PMAX_MAP"};
    for (int m = 0; m < 3; m++)
    {
        if (!has(maps[m]))
            continue;
        vector<string> &v = cfg[maps[m]];
        int nk = atoi(v[0].c_str());
        for (int i = 0; i < nk; i++)
        {
            uint64_t r = strtoull(v[1 + 2 * i].c_str(), 0, 10);
            if (m == 0)
                rate2kmax[r] = atoi(v[2 + 2 * i].c_str());
            else if (m == 1)
                rate2kmin[r] = atoi(v[2 + 2 * i].c_str());
            else
                rate2pmax[r] = atof(v[2 + 2 * i].c_str());
        }
    }

    Config::SetDefault("ns3::QbbNetDevice::PauseTime", UintegerValue(pause_time));
    Config::SetDefault("ns3::QbbNetDevice::QcnEnabled", BooleanValue(enable_qcn));
    Config::SetDefault("ns3::QbbNetDevice::DynamicThreshold", BooleanValue(dynamicth));
    IntHop::multi = int_multi;
    if (cc_mode == 7)
        IntHeader::mode = IntHeader::TS;
    else if (cc_mode == 3)
        IntHeader::mode = IntHeader::NORMAL;
    else if (cc_mode == 10)
        IntHeader::mode = IntHeader::PINT;
    else
        IntHeader::mode = IntHeader::NONE;

    // topology
    ifstream topof(topo_file.c_str());
    if (!topof)
    {
        fprintf(stderr, "cannot open %s\n", topo_file.c_str());
        return 1;
    }
    uint32_t H = 0, S = 0;
    vector<pair<uint32_t, uint32_t> > links;
    {
        string w, a, b;
        while (topof >> w)
        {
            if (w == "hosts")
                topof >> H;
            else if (w == "switches")
                topof >> S;
            else if (w == "link")
            {
                topof >> a >> b;
                auto id = [&](const string &x) { return (x[0] == 'h' ? 0 : H) + (uint32_t)atoi(x.c_str() + 1); };
                links.push_back(make_pair(id(a), id(b)));
            }
        }
    }
    uint32_t node_num = H + S;
    for (uint32_t i = 0; i < node_num; i++)
    {
        if (i < H)
            n.Add(CreateObject<Node>());
        else
        {
            Ptr<SwitchNode> sw = CreateObject<SwitchNode>();
            n.Add(sw);
            sw->SetAttribute("EcnEnabled", BooleanValue(enable_qcn));
        }
    }
    InternetStackHelper internet;
    internet.Install(n);
    vector<Ipv4Address> serverAddress(H);
    for (uint32_t i = 0; i < H; i++)
        serverAddress[i] = node_id_to_ip(i);

    Ptr<RateErrorModel> rem = CreateObject<RateErrorModel>();
    Ptr<UniformRandomVariable> uv = CreateObject<UniformRandomVariable>();
    rem->SetRandomVariable(uv);
    uv->SetStream(50);
    rem->SetAttribute("ErrorRate", DoubleValue(0.0));
    rem->SetAttribute("ErrorUnit", StringValue("ERROR_UNIT_PACKET"));
    if (!tx_file.empty())
    {
        tx_out = fopen(tx_file.c_str(), "w");
        if (tx_until == 0)
            tx_until = ~0ull;
    }
    QbbHelper qbb;
    Ipv4AddressHelper ipv4;
    for (uint32_t i = 0; i < links.size(); i++)
    {
        uint32_t src = links[i].first, dst = links[i].second;
        Ptr<Node> snode = n.Get(src), dnode = n.Get(dst);
        qbb.SetDeviceAttribute("DataRate", StringValue(rate));
        qbb.SetChannelAttribute("Delay", StringValue(delay));
        qbb.SetDeviceAttribute("ReceiveErrorModel", PointerValue(rem));
        NetDeviceContainer d = qbb.Install(snode, dnode);
        if (snode->GetNodeType() == 0)
        {
            Ptr<Ipv4> ip = snode->GetObject<Ipv4>();
            ip->AddInterface(d.Get(0));
            ip->AddAddress(1, Ipv4InterfaceAddress(serverAddress[src], Ipv4Mask(0xff000000)));
        }
        if (dnode->GetNodeType() == 0)
        {
            Ptr<Ipv4> ip = dnode->GetObject<Ipv4>();
            ip->AddInterface(d.Get(1));
            ip->AddAddress(1, Ipv4InterfaceAddress(serverAddress[dst], Ipv4Mask(0xff000000)));
        }
        for (int e = 0; e < 2; e++)
        {
            Ptr<QbbNetDevice> dev = DynamicCast<QbbNetDevice>(d.Get(e));
            Interface &itf = e == 0 ? nbr2if[snode][dnode] : nbr2if[dnode][snode];
            itf.idx = dev->GetIfIndex();
            itf.up = true;
            itf.delay = DynamicCast<QbbChannel>(dev->GetChannel())->GetDelay().GetTimeStep();
            itf.bw = dev->GetDataRate().GetBitRate();
            if (tx_out)
                dev->TraceConnectWithoutContext("PhyTxBegin",
                                                MakeBoundCallback(&tx_begin, dev->GetNode()->GetId(), dev->GetIfIndex()));
        }
        char ipstring[16];
        sprintf(ipstring, "10.%d.%d.0", i / 254 + 1, i % 254 + 1);
        ipv4.SetBase(ipstring, "255.255.255.0");
        ipv4.Assign(d);
    }

    uint64_t nic_rate = DynamicCast<QbbNetDevice>(n.Get(0)->GetDevice(1))->GetDataRate().GetBitRate();
    for (uint32_t i = H; i < node_num; i++)
    {
        Ptr<SwitchNode> sw = DynamicCast<SwitchNode>(n.Get(i));
        uint32_t shift = 3;
        for (uint32_t j = 1; j < sw->GetNDevices(); j++)
        {
            Ptr<QbbNetDevice> dev = DynamicCast<QbbNetDevice>(sw->GetDevice(j));
            uint64_t r = dev->GetDataRate().GetBitRate();
            if (!rate2kmin.count(r) || !rate2kmax.count(r) || !rate2pmax.count(r))
            {
                fprintf(stderr, "KMIN/KMAX/PMAX_MAP: no entry for %lu bps\n", r);
                return 1;
            }
            sw->m_mmu->ConfigEcn(j, rate2kmin[r], rate2kmax[r], rate2pmax[r]);
            sw->m_mmu->ConfigEcnStream(j, (uint64_t)i * SwitchMmu::pCnt + j);
            uint64_t dl = DynamicCast<QbbChannel>(dev->GetChannel())->GetDelay().GetTimeStep();
            uint32_t headroom = r * dl / 8 / 1000000000 * 3;
            sw->m_mmu->ConfigHdrm(j, headroom);
            sw->m_mmu->pfc_a_shift[j] = shift;
            while (r > nic_rate && sw->m_mmu->pfc_a_shift[j] > 0)
            {
                sw->m_mmu->pfc_a_shift[j]--;
                r /= 2;
            }
        }
        sw->m_mmu->ConfigNPort(sw->GetNDevices() - 1);
        sw->m_mmu->ConfigBufferSize(buffer_size * 1024 * 1024);
        sw->m_mmu->node_id = sw->GetId();
    }

    fct_out = fopen(out_file.c_str(), "w");
    for (uint32_t i = 0; i < H; i++)
    {
        Ptr<RdmaHw> rdmaHw = CreateObject<RdmaHw>();
        rdmaHw->SetAttribute("ClampTargetRate", BooleanValue(clamp_target_rate));
        rdmaHw->SetAttribute("AlphaResumInterval", DoubleValue(alpha_resume_interval));
        rdmaHw->SetAttribute("RPTimer", DoubleValue(rp_timer));
        rdmaHw->SetAttribute("FastRecoveryTimes", UintegerValue(fast_recovery_times));
        rdmaHw->SetAttribute("EwmaGain", DoubleValue(ewma_gain));
        rdmaHw->SetAttribute("RateAI", DataRateValue(DataRate(rate_ai)));
        rdmaHw->SetAttribute("RateHAI", DataRateValue(DataRate(rate_hai)));
        rdmaHw->SetAttribute("L2BackToZero", BooleanValue(l2_back_to_zero));
        rdmaHw->SetAttribute("L2ChunkSize", UintegerValue(l2_chunk_size));
        rdmaHw->SetAttribute("L2AckInterval", UintegerValue(l2_ack_interval));
        rdmaHw->SetAttribute("CcMode", UintegerValue(cc_mode));
        rdmaHw->SetAttribute("RateDecreaseInterval", DoubleValue(rate_decrease_interval));
        rdmaHw->SetAttribute("MinRate", DataRateValue(DataRate(min_rate)));
        rdmaHw->SetAttribute("Mtu", UintegerValue(packet_payload_size));
        rdmaHw->SetAttribute("MiThresh", UintegerValue(mi_thresh));
        rdmaHw->SetAttribute("VarWin", BooleanValue(var_win));
        rdmaHw->SetAttribute("FastReact", BooleanValue(fast_react));
        rdmaHw->SetAttribute("MultiRate", BooleanValue(multi_rate));
        rdmaHw->SetAttribute("SampleFeedback", BooleanValue(sample_feedback));
        rdmaHw->SetAttribute("TargetUtil", DoubleValue(u_target));
        rdmaHw->SetAttribute("RateBound", BooleanValue(rate_bound));
        rdmaHw->SetAttribute("DctcpRateAI", DataRateValue(DataRate(dctcp_rate_ai)));
        Ptr<RdmaDriver> rdma = CreateObject<RdmaDriver>();
        Ptr<Node> node = n.Get(i);
        rdma->SetNode(node);
        rdma->SetRdmaHw(rdmaHw);
        node->AggregateObject(rdma);
        rdma->Init();
        rdma->TraceConnectWithoutContext("QpComplete", MakeBoundCallback(qp_finish, fct_out));
    }
    RdmaEgressQueue::ack_q_idx = ack_high_prio ? 0 : 3;

    for (uint32_t i = 0; i < H; i++)
        CalculateRoute(n.Get(i));
    SetRoutingEntries();
    maxRtt = maxBdp = 0;
    for (uint32_t i = 0; i < H; i++)
        for (uint32_t j = 0; j < H; j++)
        {
            uint64_t dl = pairDelay[n.Get(i)][n.Get(j)], txd = pairTxDelay[n.Get(i)][n.Get(j)];
            uint64_t rtt = dl * 2 + txd, bw = pairBw[i][j];
            uint64_t bdp = rtt * bw / 1000000000 / 8;
            pairBdp[n.Get(i)][n.Get(j)] = bdp;
            pairRtt[i][j] = rtt;
            maxBdp = std::max(maxBdp, bdp);
            maxRtt = std::max(maxRtt, rtt);
        }
    printf("maxRtt=%lu maxBdp=%lu\n", maxRtt, maxBdp);
    for (uint32_t i = H; i < node_num; i++)
    {
        Ptr<SwitchNode> sw = DynamicCast<SwitchNode>(n.Get(i));
        sw->SetAttribute("CcMode", UintegerValue(cc_mode));
        sw->SetAttribute("MaxRtt", UintegerValue(maxRtt));
    }
    if (getenv("DUMP_ROUTES"))
    {
        // a switch's equal-cost ports towards each host, in table order
        FILE *rf = fopen(getenv("DUMP_ROUTES"), "w");
        for (auto &i : nextHop)
            if (i.first->GetNodeType() == 1)
                for (auto &j : i.second)
                {
                    fprintf(rf, "%u %u", i.first->GetId(), j.first->GetId());
                    for (Ptr<Node> nx : j.second)
                        fprintf(rf, " %u", nbr2if[i.first][nx].idx);
                    fprintf(rf, "\n");
                }
        fclose(rf);
    }

    // flows
    ifstream ff(flow_file.c_str());
    if (!ff)
    {
        fprintf(stderr, "cannot open %s\n", flow_file.c_str());
        return 1;
    }
    string line;
    getline(ff, line); // header
    while (getline(ff, line))
    {
        if (line.empty())
            continue;
        Flow f;
        unsigned long long sz, st;
        if (sscanf(line.c_str(), "%u,%u,%llu,%llu", &f.src, &f.dst, &sz, &st) != 4)
            continue;
        f.size = sz;
        f.start = st;
        f.id = flows.size();
        flows.push_back(f);
    }
    stable_sort(flows.begin(), flows.end(), [](const Flow &a, const Flow &b) { return a.start < b.start; });
    for (uint32_t i = 0; i < H; i++)
        for (uint32_t j = 0; j < H; j++)
            portNumber[i][j] = 10000;
    next_flow = 0;
    if (!flows.empty())
        Simulator::Schedule(NanoSeconds(flows[0].start), ScheduleFlowInputs);

    Simulator::Stop(Seconds(num("SIMULATOR_STOP_TIME", 10.0)));
    Simulator::Run();
    Simulator::Destroy();
    fclose(fct_out);
    if (tx_out)
        fclose(tx_out);
    printf("flows done %u of %zu\n", flows_done, flows.size());
    return 0;
}
