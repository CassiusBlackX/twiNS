// ns-3 reference scenario for TwiNS (and the GeDES model) on identical flows: FatTree or leaf-spine.
//
// Topology and parameters follow GeDES: FatTree-k, 100 Gbps links, 1 us propagation delay,
// MSS 1460, switch egress queues of 200 packets with ECN marking above K packets (RED with
// MinTh = MaxTh = K, instantaneous queue), ECMP (per-packet random by default, like GeDES's
// per-slot spraying), initial cwnd 10, minimum RTO 1 ms.
//
// Flow file rows: "node,flow_size,start_ns" (GeDES pairing: node sends to node + N/2) or
// "src,dst,flow_size,start_ns". Every flow is its own TCP connection. The FCT is measured the
// way GeDES does: from the moment the sender can send (connection established; GeDES assumes
// established connections) to the arrival of the ACK covering the last byte.
// Output CSV: src,dst,flow_size,start_ns,established_ns,completed_ns
#include "ns3/applications-module.h"
#include "ns3/core-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/traffic-control-module.h"
#include "ns3/hash.h"
#ifdef NS3_MTP
#include "ns3/mtp-module.h"
#endif

#include <atomic>
#include <map>
#include <chrono>
#include <fstream>
#include <sstream>

using namespace ns3;

struct FlowRec
{
    int src, dst;
    uint64_t size;
    int64_t start_ns;
    int64_t est_ns = -1;
    int64_t done_ns = -1;
    uint32_t hash_data = 0, hash_ack = 0; // ns-3 flow hashes of both directions (ECMP)
    uint32_t retx = 0;
};

static std::vector<FlowRec> g_flows;
static std::atomic<uint64_t> g_drops{0}, g_marks{0}, g_retx{0};
static std::atomic<uint64_t> g_retx_state[5], g_rto{0}; // retransmissions by TcpCongState_t; RTOs

// Sends one flow over its own TCP connection and records connection and completion times.
class FlowSender : public Application
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("FlowSender").SetParent<Application>().AddConstructor<FlowSender>();
        return tid;
    }

    void Setup(uint32_t flow, Address peer)
    {
        m_flow = flow;
        m_peer = peer;
    }

  private:
    void StartApplication() override
    {
        m_socket = Socket::CreateSocket(GetNode(), TcpSocketFactory::GetTypeId());
        m_socket->Bind();
        {
            // the flow hash Ipv4QueueDiscItem::Hash(0) of both directions, for TwiNS's ECMP (--ecmp_from)
            Address local;
            m_socket->GetSockName(local);
            uint16_t sport = InetSocketAddress::ConvertFrom(local).GetPort();
            Ipv4Address src = GetNode()->GetObject<Ipv4>()->GetAddress(1, 0).GetLocal();
            Ipv4Address dst = InetSocketAddress::ConvertFrom(m_peer).GetIpv4();
            uint16_t dport = InetSocketAddress::ConvertFrom(m_peer).GetPort();
            auto h = [](Ipv4Address a, Ipv4Address b, uint16_t pa, uint16_t pb) {
                uint8_t buf[17] = {0};
                a.Serialize(buf);
                b.Serialize(buf + 4);
                buf[8] = 6;
                buf[9] = (pa >> 8) & 0xff;
                buf[10] = pa & 0xff;
                buf[11] = (pb >> 8) & 0xff;
                buf[12] = pb & 0xff;
                return Hash32((char *)buf, sizeof(buf));
            };
            g_flows[m_flow].hash_data = h(src, dst, sport, dport);
            g_flows[m_flow].hash_ack = h(dst, src, dport, sport);
        }
        m_socket->SetConnectCallback(MakeCallback(&FlowSender::Connected, this),
                                     MakeCallback(&FlowSender::Failed, this));
        m_socket->SetSendCallback(MakeCallback(&FlowSender::Fill, this));
        m_socket->TraceConnectWithoutContext("HighestRxAck", MakeCallback(&FlowSender::Acked, this));
        m_socket->TraceConnectWithoutContext("Retransmission", MakeCallback(&FlowSender::Retx, this));
        m_socket->TraceConnectWithoutContext("CongState", MakeCallback(&FlowSender::State, this));
        const char *tf = getenv("TRACE_FLOW");
        if ((tf && (uint32_t)atoi(tf) == m_flow) || (!tf && m_flow == 0 && getenv("TRACE_CWND")))
        {
            m_socket->TraceConnectWithoutContext("Tx", MakeCallback(&FlowSender::TxTrace, this));
            m_socket->TraceConnectWithoutContext("Rx", MakeCallback(&FlowSender::RxTrace, this));
            PointerValue cc;
            m_socket->GetAttribute("CongestionOps", cc);
            if (Ptr<TcpDctcp> d = DynamicCast<TcpDctcp>(cc.Get<TcpCongestionOps>()))
                d->TraceConnectWithoutContext(
                    "CongestionEstimate", MakeCallback(+[](uint32_t ecn, uint32_t total, double alpha) {
                        std::cout << "alpha " << Simulator::Now().GetNanoSeconds() << " " << ecn << " " << total << " "
                                  << std::hexfloat << alpha << std::defaultfloat << std::endl;
                    }));
            m_socket->TraceConnectWithoutContext("CongestionWindow", MakeCallback(&FlowSender::Cwnd, this));
            m_socket->TraceConnectWithoutContext("HighestRxAck", MakeCallback(&FlowSender::AckTrace, this));
        }
        m_socket->Connect(m_peer);
    }

    void StopApplication() override
    {
    }

    void Connected(Ptr<Socket> s)
    {
        g_flows[m_flow].est_ns = Simulator::Now().GetNanoSeconds();
        Fill(s, s->GetTxAvailable());
    }

    void Failed(Ptr<Socket>)
    {
        std::cerr << "connection failed for flow " << m_flow << std::endl;
    }

    void Fill(Ptr<Socket> s, uint32_t)
    {
        uint64_t size = g_flows[m_flow].size;
        while (m_sent < size)
        {
            uint32_t avail = s->GetTxAvailable();
            if (avail == 0)
                break;
            uint32_t n = (uint32_t)std::min<uint64_t>({size - m_sent, avail, 1u << 20});
            int r = s->Send(Create<Packet>(n));
            if (r <= 0)
                break;
            m_sent += r;
        }
    }

    void Acked(SequenceNumber32, SequenceNumber32 now)
    {
        // the SYN takes sequence number 0, data occupies 1..size
        if (g_flows[m_flow].done_ns < 0 && now.GetValue() >= g_flows[m_flow].size + 1)
            g_flows[m_flow].done_ns = Simulator::Now().GetNanoSeconds();
    }

    void Cwnd(uint32_t, uint32_t now)
    {
        std::cout << "cwnd " << Simulator::Now().GetNanoSeconds() << " " << now << std::endl;
    }

    void AckTrace(SequenceNumber32, SequenceNumber32 now)
    {
        std::cout << "ack " << Simulator::Now().GetNanoSeconds() << " " << now.GetValue() << std::endl;
    }

    void TxTrace(Ptr<const Packet> p, const TcpHeader &h, Ptr<const TcpSocketBase>)
    {
        if (p->GetSize() == 0)
            return;
        uint32_t seq = h.GetSequenceNumber().GetValue();
        std::cout << "tx " << Simulator::Now().GetNanoSeconds() << " " << seq << (seq < m_high ? " R" : "") << " st " << m_state
                  << std::endl;
        m_high = std::max(m_high, seq + p->GetSize());
    }

    void RxTrace(Ptr<const Packet>, const TcpHeader &h, Ptr<const TcpSocketBase>)
    {
        std::cout << "rx " << Simulator::Now().GetNanoSeconds() << " ack " << h.GetAckNumber().GetValue()
                  << ((h.GetFlags() & TcpHeader::ECE) ? " ECE" : "") << std::endl;
    }

    void Retx(Ptr<const Packet>, const TcpHeader &, const Address &, const Address &, Ptr<const TcpSocketBase>)
    {
        g_retx++;
        g_flows[m_flow].retx++;
        g_retx_state[m_state < 5 ? m_state : 4]++;
    }

    void State(TcpSocketState::TcpCongState_t, TcpSocketState::TcpCongState_t now)
    {
        // the RTO retransmission is traced after the switch to CA_LOSS
        if (now == TcpSocketState::CA_LOSS && m_state != TcpSocketState::CA_LOSS)
            g_rto++;
        m_state = now;
    }

    uint32_t m_state = 0;
    uint32_t m_high = 0;
    uint32_t m_flow = 0;
    Address m_peer;
    Ptr<Socket> m_socket;
    uint64_t m_sent = 0;
};

int main(int argc, char *argv[])
{
    uint32_t k = 4;
    std::string flowFile, out = "fct.csv", tcp = "ns3::TcpNewReno";
    uint32_t threads = 0;
    uint32_t ecnK = 30;
    uint32_t queuePkts = 200;
    bool perFlowEcmp = false;
    double stopSec = 1.0;
    uint32_t dupThresh = 3;
    std::string hostQ = "default"; // host egress: default (pfifo_fast 1000p), red (like switches), big (no local drops)
    CommandLine cmd;
    cmd.AddValue("k", "FatTree k", k);
    std::string topo = "fattree";
    uint32_t leaves = 16, spines = 8, hpl = 16;
    cmd.AddValue("topo", "fattree or leafspine", topo);
    cmd.AddValue("leaves", "leaf-spine: leaf switches", leaves);
    cmd.AddValue("spines", "leaf-spine: spine switches (each leaf connects to every spine)", spines);
    cmd.AddValue("hpl", "leaf-spine: hosts per leaf", hpl);
    cmd.AddValue("flows", "flow file", flowFile);
    cmd.AddValue("out", "output CSV", out);
    cmd.AddValue("tcp", "TCP type", tcp);
    cmd.AddValue("threads", "Unison threads (0: sequential)", threads);
    cmd.AddValue("ecnK", "ECN marking threshold (packets)", ecnK);
    cmd.AddValue("queue", "switch egress queue (packets)", queuePkts);
    cmd.AddValue("flowEcmp", "per-flow instead of per-packet ECMP", perFlowEcmp);
    cmd.AddValue("stop", "simulation stop time (s)", stopSec);
    cmd.AddValue("dupthresh", "duplicate ACKs that trigger fast retransmit", dupThresh);
    cmd.AddValue("hostq", "host egress queue: default, red, big", hostQ);
    cmd.Parse(argc, argv);

#ifdef NS3_MTP
    if (threads)
        MtpInterface::Enable(threads);
#endif
    Time::SetResolution(Time::PS);

    // ---- model parameters (aligned with GeDES)
    Config::SetDefault("ns3::PointToPointChannel::Delay", TimeValue(NanoSeconds(1000)));
    Config::SetDefault("ns3::PointToPointNetDevice::DataRate", StringValue("100Gbps"));
    Config::SetDefault("ns3::PointToPointNetDevice::Mtu", UintegerValue(1500));
    Config::SetDefault("ns3::RedQueueDisc::UseEcn", BooleanValue(true));
    Config::SetDefault("ns3::RedQueueDisc::UseHardDrop", BooleanValue(false));
    Config::SetDefault("ns3::RedQueueDisc::Gentle", BooleanValue(false));
    Config::SetDefault("ns3::RedQueueDisc::QW", DoubleValue(1.0));
    Config::SetDefault("ns3::RedQueueDisc::MeanPktSize", UintegerValue(1500));
    Config::SetDefault("ns3::RedQueueDisc::MinTh", DoubleValue(ecnK));
    Config::SetDefault("ns3::RedQueueDisc::MaxTh", DoubleValue(ecnK));
    Config::SetDefault("ns3::RedQueueDisc::MaxSize", QueueSizeValue(QueueSize(std::to_string(queuePkts) + "p")));
    Config::SetDefault("ns3::Ipv4GlobalRouting::RandomEcmpRouting", BooleanValue(true));
    Config::SetDefault("ns3::Ipv4GlobalRouting::FlowEcmpRouting", BooleanValue(perFlowEcmp));
    Config::SetDefault("ns3::TcpL4Protocol::SocketType", StringValue(tcp));
    Config::SetDefault("ns3::TcpSocket::SegmentSize", UintegerValue(1460));
    Config::SetDefault("ns3::TcpSocket::InitialCwnd", UintegerValue(10));
    Config::SetDefault("ns3::TcpSocket::SndBufSize", UintegerValue(1 << 30));
    Config::SetDefault("ns3::TcpSocket::RcvBufSize", UintegerValue(1 << 30));
    Config::SetDefault("ns3::TcpSocket::DelAckCount", UintegerValue(2));
    Config::SetDefault("ns3::TcpSocket::DelAckTimeout", TimeValue(MicroSeconds(10)));
    Config::SetDefault("ns3::TcpSocket::ConnTimeout", TimeValue(MilliSeconds(10)));
    Config::SetDefault("ns3::TcpSocketBase::MinRto", TimeValue(MilliSeconds(1)));
    Config::SetDefault("ns3::TcpSocketBase::ClockGranularity", TimeValue(MicroSeconds(1)));
    Config::SetDefault("ns3::TcpSocketBase::Timestamp", BooleanValue(false));
    Config::SetDefault("ns3::TcpSocketBase::Sack", BooleanValue(false));
    Config::SetDefault("ns3::RttEstimator::InitialEstimation", TimeValue(MicroSeconds(100)));
    Config::SetDefault("ns3::TcpSocketBase::ReTxThreshold", UintegerValue(dupThresh));
    // reference model: classic NewReno recovery, no limited transmit (TwiNS implements these)
    Config::SetDefault("ns3::TcpSocketBase::LimitedTransmit", BooleanValue(false));
    Config::SetDefault("ns3::TcpL4Protocol::RecoveryType", TypeIdValue(TcpClassicRecovery::GetTypeId()));
    if (tcp == "ns3::TcpDctcp")
        Config::SetDefault("ns3::TcpSocketBase::UseEcn", StringValue("On"));

    // ---- flows
    bool leafSpine = topo == "leafspine";
    uint32_t H = leafSpine ? leaves * hpl : k * k * k / 4;
    {
        std::ifstream in(flowFile);
        std::string line;
        while (std::getline(in, line))
        {
            if (line.empty() || !isdigit(line[0]))
                continue;
            std::stringstream ss(line);
            std::string f;
            std::vector<int64_t> v;
            while (std::getline(ss, f, ','))
                v.push_back(std::stoll(f));
            FlowRec r;
            if (v.size() == 3)
                r = {(int)v[0], (int)((v[0] + H / 2) % H), (uint64_t)v[1], v[2]};
            else
                r = {(int)v[0], (int)v[1], (uint64_t)v[2], v[3]};
            g_flows.push_back(r);
        }
    }

    // ---- topology. FatTree: hosts numbered as in GeDES (pod, edge, host); nodes are hosts, edges,
    // aggregations, cores. Leaf-spine: host h on leaf h / hpl; nodes are hosts, leaves, spines.
    // Devices are installed host links first, so a switch's host ports come before its uplinks.
    uint32_t hk = k / 2;
    NodeContainer hosts, edges, aggs, cores;
    hosts.Create(H);
    if (leafSpine)
    {
        edges.Create(leaves); // the leaves
        cores.Create(spines); // the spines
    }
    else
    {
        edges.Create(k * hk);
        aggs.Create(k * hk);
        cores.Create(hk * hk);
    }
    InternetStackHelper internet;
    internet.SetIpv6StackInstall(false);
    internet.InstallAll();
    PointToPointHelper p2p;
    p2p.SetQueue("ns3::DropTailQueue<Packet>", "MaxSize", StringValue("1p")); // queueing in the qdisc
    TrafficControlHelper red;
    red.SetRootQueueDisc("ns3::RedQueueDisc");
    TrafficControlHelper bigq;
    bigq.SetRootQueueDisc("ns3::FifoQueueDisc", "MaxSize", QueueSizeValue(QueueSize("1000000p")));
    Ipv4AddressHelper addr;
    std::vector<Ipv4Address> hostAddr(H);
    uint32_t subnet = 0;
    auto nextSubnet = [&]() {
        std::ostringstream s;
        s << "10." << (subnet >> 8) << "." << (subnet & 255) << ".0";
        subnet++;
        addr.SetBase(s.str().c_str(), "255.255.255.0");
    };
    for (uint32_t h = 0; h < H; h++)
    {
        uint32_t e = leafSpine ? h / hpl : h / hk;
        NetDeviceContainer d = p2p.Install(hosts.Get(h), edges.Get(e));
        red.Install(d.Get(1)); // switch side
        if (hostQ == "red")
            red.Install(d.Get(0));
        else if (hostQ == "big")
            bigq.Install(d.Get(0));
        nextSubnet();
        hostAddr[h] = addr.Assign(d).GetAddress(0);
    }
    if (leafSpine)
        for (uint32_t l = 0; l < leaves; l++)
            for (uint32_t sp = 0; sp < spines; sp++)
            {
                NetDeviceContainer d = p2p.Install(edges.Get(l), cores.Get(sp));
                red.Install(d);
                nextSubnet();
                addr.Assign(d);
            }
    for (uint32_t pod = 0; pod < (leafSpine ? 0 : k); pod++)
        for (uint32_t a = 0; a < hk; a++)
            for (uint32_t e = 0; e < hk; e++)
            {
                NetDeviceContainer d = p2p.Install(aggs.Get(pod * hk + a), edges.Get(pod * hk + e));
                red.Install(d);
                nextSubnet();
                addr.Assign(d);
            }
    for (uint32_t i = 0; i < (leafSpine ? 0 : hk); i++)
        for (uint32_t j = 0; j < hk; j++)
            for (uint32_t pod = 0; pod < k; pod++)
            {
                // core (i, j) connects to aggregation switch i of every pod
                NetDeviceContainer d = p2p.Install(cores.Get(i * hk + j), aggs.Get(pod * hk + i));
                red.Install(d);
                nextSubnet();
                addr.Assign(d);
            }
    Ipv4GlobalRoutingHelper::PopulateRoutingTables();
    for (NodeList::Iterator it = NodeList::Begin(); it != NodeList::End(); ++it)
    {
        Ptr<TrafficControlLayer> tc = (*it)->GetObject<TrafficControlLayer>();
        for (uint32_t d = 0; d < (*it)->GetNDevices(); d++)
        {
            Ptr<QueueDisc> q = tc ? tc->GetRootQueueDiscOnDevice((*it)->GetDevice(d)) : nullptr;
            if (!q || !DynamicCast<RedQueueDisc>(q))
                continue;
            q->TraceConnectWithoutContext("Drop", MakeBoundCallback(+[](int, Ptr<const QueueDiscItem>) { g_drops++; }, 0));
            if (const char *ta = getenv("TRACE_ALLQ"))
            {
                // TRACE_ALLQ=<ps>: every enqueue before <ps>, in TwiNS's qtrace format
                static int64_t until = atoll(ta);
                static std::map<uint32_t, int> hostOf;
                if (hostOf.empty())
                    for (uint32_t h = 0; h < H; h++)
                        hostOf[hostAddr[h].Get()] = h;
                uint32_t node = (*it)->GetId(), dev = d;
                Ptr<QueueDisc> qq = q;
                q->TraceConnectWithoutContext(
                    "Enqueue", MakeBoundCallback(+[](Ptr<QueueDisc> qq, uint32_t node, uint32_t dev, Ptr<const QueueDiscItem> item) {
                        static int64_t from = getenv("TRACE_ALLQ_FROM") ? atoll(getenv("TRACE_ALLQ_FROM")) : 0;
                        if (Simulator::Now().GetPicoSeconds() >= until || Simulator::Now().GetPicoSeconds() < from)
                            return;
                        auto ip = DynamicCast<const Ipv4QueueDiscItem>(item);
                        if (!ip)
                            return;
                        TcpHeader th;
                        item->GetPacket()->PeekHeader(th);
                        uint32_t pl = item->GetPacket()->GetSize() - th.GetSerializedSize();
                        uint8_t fl = th.GetFlags();
                        const char *kind = pl ? "D" : (fl & TcpHeader::SYN) ? ((fl & TcpHeader::ACK) ? "SA" : "S") : "A";
                        uint32_t sa = pl ? th.GetSequenceNumber().GetValue() - 1 : th.GetAckNumber().GetValue() - 1;
                        if (fl & TcpHeader::SYN)
                            sa = 0;
                        std::cout << "Q " << Simulator::Now().GetPicoSeconds() << " " << node << " " << dev << " "
                                  << hostOf[ip->GetHeader().GetSource().Get()] << " " << hostOf[ip->GetHeader().GetDestination().Get()]
                                  << " " << kind << " " << sa << " " << qq->GetNPackets() << std::endl;
                    }, qq, node, dev));
            }
            if (const char *tq = getenv("TRACE_QUEUE"))
            {
                // TRACE_QUEUE=<src host>,<dst host>: enqueue events of that flow's data packets
                static Ipv4Address qs, qd;
                int a = 0, b = 0;
                sscanf(tq, "%d,%d", &a, &b);
                qs = hostAddr[a];
                qd = hostAddr[b];
                uint32_t node = (*it)->GetId(), dev = d;
                Ptr<QueueDisc> qq = q;
                q->TraceConnectWithoutContext(
                    "Enqueue", MakeBoundCallback(+[](Ptr<QueueDisc> qq, uint32_t node, uint32_t dev, Ptr<const QueueDiscItem> item) {
                        auto ip = DynamicCast<const Ipv4QueueDiscItem>(item);
                        if (!ip)
                            return;
                        static int tn = getenv("TRACE_NODE") ? atoi(getenv("TRACE_NODE")) : -1;
                        static int td = getenv("TRACE_DEV") ? atoi(getenv("TRACE_DEV")) : -1;
                        bool here = (int)node == tn && (int)dev == td;
                        TcpHeader th;
                        item->GetPacket()->PeekHeader(th);
                        bool data = item->GetPacket()->GetSize() > th.GetSerializedSize();
                        static bool all = getenv("TRACE_ALL") != nullptr;
                        bool fwd = ip->GetHeader().GetSource() == qs && ip->GetHeader().GetDestination() == qd;
                        bool rev = ip->GetHeader().GetSource() == qd && ip->GetHeader().GetDestination() == qs;
                        bool mine = all ? (fwd || rev) : (data && fwd);
                        if (!mine && !here)
                            return;
                        std::cout << "enq " << Simulator::Now().GetPicoSeconds() << " node " << node << " dev " << dev << " flags "
                                  << (int)th.GetFlags() << " src "
                                  << ip->GetHeader().GetSource() << " seq " << th.GetSequenceNumber().GetValue() << " ack "
                                  << th.GetAckNumber().GetValue() << " qlen-after " << qq->GetNPackets() << " ecn "
                                  << (int)ip->GetHeader().GetEcn() << " len " << item->GetSize() << std::endl;
                    }, qq, node, dev));
            }
            q->TraceConnectWithoutContext("Mark", MakeBoundCallback(+[](int, Ptr<const QueueDiscItem>, const char *) { g_marks++; }, 0));
        }
    }

    // ---- applications: one sink per destination host, one sender per flow
    const uint16_t port = 1000;
    std::vector<bool> hasSink(H, false);
    for (auto &f : g_flows)
        hasSink[f.dst] = true;
    PacketSinkHelper sink("ns3::TcpSocketFactory", InetSocketAddress(Ipv4Address::GetAny(), port));
    for (uint32_t h = 0; h < H; h++)
        if (hasSink[h])
            sink.Install(hosts.Get(h)).Start(Seconds(0));
    for (uint32_t i = 0; i < g_flows.size(); i++)
    {
        Ptr<FlowSender> app = CreateObject<FlowSender>();
        app->Setup(i, InetSocketAddress(hostAddr[g_flows[i].dst], port));
        hosts.Get(g_flows[i].src)->AddApplication(app);
        app->SetStartTime(NanoSeconds(g_flows[i].start_ns));
    }

    if (const char *tr = getenv("TRACE_RX"))
    {
        // TRACE_RX=<dst host>,<src host>,<ns>: at <ns>, trace the receiving socket of src->dst
        static Ipv4Address peer;
        int d = 0, sh = 0;
        long long at = 0;
        sscanf(tr, "%d,%d,%lld", &d, &sh, &at);
        peer = hostAddr[sh];
        uint32_t nodeId = hosts.Get(d)->GetId();
        Simulator::Schedule(NanoSeconds(at), [nodeId]() {
            Config::MatchContainer m =
                Config::LookupMatches("/NodeList/" + std::to_string(nodeId) + "/$ns3::TcpL4Protocol/SocketList/*");
            for (uint32_t i = 0; i < m.GetN(); i++)
            {
                Ptr<TcpSocketBase> sk = DynamicCast<TcpSocketBase>(m.Get(i));
                Address a;
                if (!sk || sk->GetPeerName(a) != 0 || InetSocketAddress::ConvertFrom(a).GetIpv4() != peer)
                    continue;
                sk->TraceConnectWithoutContext(
                    "Rx", MakeCallback(+[](Ptr<const Packet> p, const TcpHeader &h, Ptr<const TcpSocketBase>) {
                        std::cout << "srv-rx " << Simulator::Now().GetPicoSeconds() << " seq " << h.GetSequenceNumber().GetValue()
                                  << " len " << p->GetSize() << " flags " << (int)h.GetFlags() << std::endl;
                    }));
                sk->TraceConnectWithoutContext(
                    "Tx", MakeCallback(+[](Ptr<const Packet> p, const TcpHeader &h, Ptr<const TcpSocketBase>) {
                        std::cout << "srv-tx " << Simulator::Now().GetPicoSeconds() << " ack " << h.GetAckNumber().GetValue()
                                  << " flags " << (int)h.GetFlags() << std::endl;
                    }));
                sk->TraceConnectWithoutContext(
                    "EcnState", MakeCallback(+[](TcpSocketState::EcnState_t o, TcpSocketState::EcnState_t n) {
                        std::cout << "srv-ecn " << Simulator::Now().GetPicoSeconds() << " " << (int)o << " -> " << (int)n << std::endl;
                    }));
                std::cout << "traced socket " << i << std::endl;
            }
        });
    }

    Simulator::Stop(Seconds(stopSec));
    auto t0 = std::chrono::steady_clock::now();
    Simulator::Run();
    double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    Simulator::Destroy();

    std::ofstream o(out);
    o << "src,dst,flow_size,start_ns,established_ns,completed_ns,hash_data,hash_ack,retx\n";
    size_t done = 0;
    for (auto &f : g_flows)
    {
        o << f.src << "," << f.dst << "," << f.size << "," << f.start_ns << "," << f.est_ns << "," << f.done_ns << ","
          << f.hash_data << "," << f.hash_ack << "," << f.retx << "\n";
        done += f.done_ns >= 0;
    }
    std::cout << "flows " << g_flows.size() << " completed " << done << " wall " << wall << " s drops " << g_drops
              << " marks " << g_marks << " retx " << g_retx << " rto " << g_rto << " | retx by state open "
              << g_retx_state[0] << " disorder " << g_retx_state[1] << " cwr " << g_retx_state[2] << " recovery "
              << g_retx_state[3] << " loss " << g_retx_state[4] << std::endl;
    return 0;
}
