// Building a World (model.h) from an ns-3-ub case (ubcase.h): node, port, TP, connection and pool
// layout, routes, the attribute subset, TP numbers in ns-3-ub's reservation order.
#pragma once
#include "model.h"
#include "ubcase.h"
#include "ubtime.h"
#include <algorithm>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace ub {

struct HostWorld {
    World w{};
    WorldMut mut{};
    std::vector<Node> node;
    std::vector<Port> port;
    std::vector<List> voq;
    std::vector<uint64_t> ingress;
    std::vector<int32_t> tplist;
    std::vector<int64_t> txt;
    std::vector<Tp> tp;
    std::vector<int32_t> tpByTpn;
    std::vector<Op> op;
    std::vector<Jetty> jetty;
    std::vector<Conn> conn;
    std::vector<Pkt> pkt;
    std::vector<Seg> seg;
    std::vector<TxEntry> ent;
    std::vector<Remote> remote;
    std::vector<TaskG> task;
    std::vector<PhaseG> phase;
    std::vector<int32_t> phaseDeps, ready;
    // routes: (node * nnode + dst) * MAXHP + dstPort -> candidates (shortest, sorted)
    static constexpr int MAXHP = 8;
    std::vector<int32_t> rtOff, rtCnt, rtPorts;
    std::vector<int32_t> rootTasks;   // tasks ready at load, in task id order

    void fill_world()
    {
        w.mut = &mut;
        w.node = node.data();
        w.port = port.data();
        w.voq = voq.data();
        w.ingress = ingress.data();
        w.tplist = tplist.data();
        w.txt = txt.data();
        w.tp = tp.data();
        w.tpByTpn = tpByTpn.data();
        w.op = op.data();
        w.jetty = jetty.data();
        w.conn = conn.data();
        w.pkt = pkt.data();
        w.seg = seg.data();
        w.ent = ent.data();
        w.remote = remote.data();
        w.task = task.data();
        w.phase = phase.data();
        w.phaseDeps = phaseDeps.data();
        w.ready = ready.data();
        w.nnode = (int32_t)node.size();
        w.ntask = (int32_t)task.size();
        w.nphase = (int32_t)phase.size();
    }
};

inline std::string check_attrs(Case &C, Params &P)
{
    auto &A = C.attr;
    std::string err;
    auto need = [&](const std::string &k, const std::string &def, const std::string &ok) {
        std::string v = A.get(k, def);
        if (v != ok && err.empty())
            err = "unsupported " + k + " = " + v + " (supported: " + ok + ")";
    };
    need("ns3::UbSwitch::FlowControl", "CBFC", "CBFC");
    need("UB_CC_ENABLED", "false", "false");
    need("ns3::UbTransportChannel::EnableRetrans", "false", "false");
    need("UB_FAULT_ENABLE", "false", "false");
    need("ns3::UbApp::EnableMultiPath", "false", "false");
    need("ns3::UbApp::RoutingType", "PER_FLOW_SHORTEST_PATHS", "PER_FLOW_SHORTEST_PATHS");
    need("ns3::UbTransportChannel::RoutingType", "PER_FLOW_SHORTEST_PATHS", "PER_FLOW_SHORTEST_PATHS");
    need("ns3::UbPort::UbInterframeGap", "+0ns", "+0ns");
    need("ns3::UbSwitch::VlScheduler", "SP", "SP");
    need("ns3::UbRoutingProcess::MultipathSelector", "HASH64", "HASH64");
    need("UB_ALLOCATOR_RR_PHASE_HASH_ENABLE", "false", "false");
    need("ns3::UbSwitchAllocator::EnableDirectAllocationCompletion", "true", "true");
    need("ns3::UbTransaction::InitiatorProtocolDispatchDelay", "+0ns", "+0ns");
    need("ns3::UbApp::EnablePlannedTpConnectionDestroy", "true", "true");
    need("ns3::UbApp::EnableTaskScopedJettyDestroy", "true", "true");
    P.vlNum = (int32_t)A.geti("UB_VL_NUM", 16);
    if (P.vlNum != 16 && err.empty())
        err = "UB_VL_NUM must be 16";
    P.cbfcInit = (int32_t)A.geti("ns3::UbPort::CbfcInitCreditCell", 6553);
    P.cbfcThld = (int32_t)A.geti("ns3::UbPort::CbfcCtrlCrdRtrThldCell", 1024);
    P.cbfcGrainData = (int32_t)A.geti("ns3::UbPort::CbfcRetCellGrainDataPacket", 4);
    P.cbfcGrainCtrl = (int32_t)A.geti("ns3::UbPort::CbfcRetCellGrainControlPacket", 32);
    P.cbfcCellBytes = (int32_t)(A.geti("ns3::UbPort::CbfcFlitLenByte", 20) * A.geti("ns3::UbPort::CbfcFlitsPerCell", 8));
    P.reservePerQueue = (uint32_t)A.geti("ns3::UbQueueManager::ReservePerQueueBytes", 1048576);
    P.inflightMax = (uint32_t)A.geti("ns3::UbTransportChannel::DefaultMaxInflightPacketSize", 1000);
    P.wqeSegMax = (uint32_t)A.geti("ns3::UbTransportChannel::DefaultMaxWqeSegNum", 1000);
    return err;
}

// returns an error message, or "" on success
inline std::string build_world(Case &C, HostWorld &H, int64_t depDelay, double poolScale = 1.0)
{
    Params P{};
    std::string err = check_attrs(C, P);
    if (!err.empty())
        return err;
    P.depDelay = depDelay;
    int NN = (int)C.nodes.size();
    // VLs in use
    std::set<int> dataVl;
    for (auto &t : C.tasks) {
        if (t.op != "URMA_WRITE")
            return "unsupported opType " + t.op;
        if (t.pri <= 0 || t.pri >= VLN)
            return "task priority must be 1..15";
        dataVl.insert(t.pri);
    }
    if ((int)dataVl.size() + 1 > MAXV)
        return "too many VLs in use";
    // nodes
    H.node.assign(NN, Node{});
    int32_t portOff = 0, voqOff = 0, ingOff = 0;
    for (int i = 0; i < NN; ++i) {
        if (C.nodes[i].ports > 64)
            return "nodes with more than 64 ports are not supported yet";
    }
    for (int i = 0; i < NN; ++i) {
        Node &n = H.node[i];
        n.id = i;
        n.sw = C.nodes[i].sw;
        n.P = C.nodes[i].ports;
        n.alloc = C.nodes[i].alloc_ps;
        n.fwd = C.nodes[i].fwd_ps;
        for (int v = 0; v < VLN; ++v) n.vlIdx[v] = -1;
        n.nv = 0;
        n.vlIdx[0] = n.nv, n.vlOf[n.nv++] = 0;
        for (int v : dataVl) n.vlIdx[v] = n.nv, n.vlOf[n.nv++] = v;
        n.port0 = portOff;
        portOff += n.P;
        n.voq0 = voqOff;
        voqOff += n.P * n.nv * n.P;
        n.ingress0 = ingOff;
        ingOff += n.P * n.nv;
    }
    H.port.assign(portOff, Port{});
    H.voq.assign(voqOff, List{-1, -1, 0});
    H.ingress.assign(ingOff, 0);
    // links and tx-time tables (one per distinct rate)
    std::map<uint64_t, int32_t> rateTab;
    const int32_t TXN = 9216;
    for (auto &l : C.links) {
        for (int side = 0; side < 2; ++side) {
            int a = side ? l.n2 : l.n1, ap = side ? l.p2 : l.p1, b = side ? l.n1 : l.n2, bp = side ? l.p1 : l.p2;
            if (ap >= H.node[a].P)
                return "topology.csv: port beyond node.csv portNum";
            Port &pt = H.port[H.node[a].port0 + ap];
            pt.peerNode = b;
            pt.peerPort = bp;
            pt.delay = l.delay_ps;
            auto it = rateTab.find(l.bps);
            if (it == rateTab.end()) {
                int32_t off = (int32_t)H.txt.size();
                H.txt.resize(off + TXN);
                for (int32_t s = 0; s < TXN; ++s)
                    H.txt[off + s] = tx_ps(l.bps, (uint32_t)s);
                it = rateTab.emplace(l.bps, off).first;
            }
            pt.txtab = it->second;
        }
    }
    H.w.txtN = TXN;
    for (auto &pt : H.port) {
        pt.active = IQ_NONE;
        pt.egress = List{-1, -1, 0};
        for (int v = 0; v < VLN; ++v) pt.txfree[v] = P.cbfcInit;
    }
    // connections in traffic.csv order (ReserveTpnsForTraffic / ReservePlannedTpnsForTraffic)
    std::map<std::tuple<int, int, int>, int> connOf;   // (a, b, pri) both directions
    std::vector<int> nextTpn(NN, 0);
    for (auto &t : C.tasks) {
        if (connOf.count({t.src, t.dst, t.pri}))
            continue;
        if (H.node[t.src].sw || H.node[t.dst].sw)
            return "tasks between switches";
        if (H.node[t.src].P != 1 || H.node[t.dst].P != 1)
            return "multi-port hosts not supported yet";
        Conn c{};
        c.n1 = t.src, c.p1 = 0, c.tpn1 = nextTpn[t.src]++;
        c.n2 = t.dst, c.p2 = 0, c.tpn2 = nextTpn[t.dst]++;
        c.pri = t.pri;
        c.ep[0] = c.ep[1] = c.ep[2] = -1;
        connOf[{t.src, t.dst, t.pri}] = connOf[{t.dst, t.src, t.pri}] = (int)H.conn.size();
        H.conn.push_back(c);
    }
    // TPs (one per connection end), remote records, jetties and operations
    std::vector<std::vector<int>> nodeTps(NN);
    std::vector<int> usesOf(H.conn.size(), 0);
    for (auto &t : C.tasks)
        usesOf[connOf[{t.src, t.dst, t.pri}]]++;
    for (size_t ci = 0; ci < H.conn.size(); ++ci) {
        Conn &c = H.conn[ci];
        c.uses = usesOf[ci];
        for (int slot = 1; slot <= 2; ++slot) {
            Tp t{};
            int me = slot == 1 ? c.n1 : c.n2, peer = slot == 1 ? c.n2 : c.n1;
            t.node = me;
            t.tpn = slot == 1 ? c.tpn1 : c.tpn2;
            t.peer = peer;
            t.peerTpn = slot == 1 ? c.tpn2 : c.tpn1;
            t.port = slot == 1 ? c.p1 : c.p2;
            t.dport = slot == 1 ? c.p2 : c.p1;
            t.pri = c.pri;
            t.vi = H.node[me].vlIdx[c.pri];
            t.sip = node_port_ip(me, t.port);
            t.dip = node_port_ip(peer, t.dport);
            t.segs = t.ackQ = t.pending = t.buffered = List{-1, -1, 0};
            t.activeRef = -1;
            t.conn = (int32_t)ci;
            t.slot = (uint8_t)slot;
            t.nremote = usesOf[ci];
            nodeTps[me].push_back((int)H.tp.size());
            H.tp.push_back(t);
        }
    }
    int32_t remOff = 0;
    for (auto &t : H.tp) {
        t.remote0 = remOff;
        remOff += t.nremote;
    }
    H.remote.assign(remOff, Remote{});
    for (auto &r : H.remote)
        r.segs = List{-1, -1, 0};
    // TPs by local number; allocator TP lists
    int32_t tpnOff = 0, tplOff = 0;
    for (int i = 0; i < NN; ++i) {
        Node &n = H.node[i];
        n.ntp = (int32_t)nodeTps[i].size();
        n.ntpn = nextTpn[i];
        n.tpByTpn0 = tpnOff;
        tpnOff += n.ntpn;
        for (int p = 0; p < n.P; ++p) {
            Port &pt = H.port[n.port0 + p];
            pt.tplist = tplOff;
            tplOff += n.nv * std::max(n.ntp, 1);
        }
        if (n.ntp == 0)
            n.ntp = 1;   // keeps the tplist stride nonzero
    }
    H.tpByTpn.assign(tpnOff, -1);
    H.tplist.assign(tplOff, -1);
    for (int i = 0; i < NN; ++i)
        for (int ti : nodeTps[i])
            H.tpByTpn[H.node[i].tpByTpn0 + H.tp[ti].tpn] = ti;
    // tasks and phases
    std::map<int, int> phaseIdx;
    for (auto &t : C.tasks)
        if (!phaseIdx.count(t.phase)) {
            int k = (int)phaseIdx.size();
            phaseIdx[t.phase] = k;
        }
    for (auto &t : C.tasks)
        for (int d : t.deps)
            if (!phaseIdx.count(d)) {
                int k = (int)phaseIdx.size();
                phaseIdx[d] = k;
            }
    H.phase.assign(phaseIdx.size(), PhaseG{});
    std::vector<std::vector<int>> deps(phaseIdx.size());
    std::vector<int> jettyCount(NN, 0);
    for (size_t i = 0; i < C.tasks.size(); ++i) {
        auto &t = C.tasks[i];
        TaskG g{};
        g.id = t.id;
        g.src = t.src;
        g.dst = t.dst;
        g.pri = t.pri;
        g.phase = phaseIdx[t.phase];
        g.conn = connOf[{t.src, t.dst, t.pri}];
        if (t.size > 0xffffffffull)
            return "task size above 4 GB";
        g.size = (uint32_t)t.size;
        g.delay = t.delay_ps;
        g.jetty = -1;
        H.phase[g.phase].total++;
        if (t.deps.empty())
            g.state = 1;
        else {
            g.pendingPhases = (int32_t)t.deps.size();
            for (int d : t.deps)
                deps[phaseIdx[d]].push_back((int)i);
        }
        jettyCount[t.src]++;
        H.task.push_back(g);
    }
    for (size_t p = 0; p < deps.size(); ++p) {
        H.phase[p].dep0 = (int32_t)H.phaseDeps.size();
        H.phase[p].ndep = (int32_t)deps[p].size();
        H.phaseDeps.insert(H.phaseDeps.end(), deps[p].begin(), deps[p].end());
    }
    H.ready.assign(C.tasks.size() + 1, 0);
    {
        std::vector<int> r;
        for (size_t i = 0; i < C.tasks.size(); ++i)
            if (H.task[i].state == 1)
                r.push_back((int)i);
        std::stable_sort(r.begin(), r.end(), [&](int a, int b) { return H.task[a].id < H.task[b].id; });
        for (size_t i = 0; i < r.size(); ++i)
            H.ready[i] = r[i];
        H.mut.nready = (int32_t)r.size();
    }
    int32_t jOff = 0;
    for (int i = 0; i < NN; ++i) {
        H.node[i].jetty0 = H.node[i].op0 = jOff;
        H.node[i].njetty = jettyCount[i];
        jOff += std::max(jettyCount[i], 1);
    }
    H.jetty.assign(jOff, Jetty{});
    for (auto &j : H.jetty) j.op = j.tp = -1;
    H.op.assign(jOff, Op{});
    for (auto &o : H.op) o.segments = List{-1, -1, 0};
    // pools: packets, segments, entries per node (free lists)
    auto mkpool = [&](auto &vec, std::vector<int32_t> sizes, auto setfree) {
        int64_t tot = 0;
        for (auto s : sizes) tot += s;
        vec.assign(tot, {});
        int64_t off = 0;
        for (int i = 0; i < NN; ++i) {
            int32_t head = -1;
            for (int64_t k = off + sizes[i] - 1; k >= off; --k) {
                vec[k].next = head;
                head = (int32_t)k;
            }
            setfree(H.node[i], head);
            off += sizes[i];
        }
    };
    std::vector<int32_t> psz(NN), ssz(NN), esz(NN);
    for (int i = 0; i < NN; ++i) {
        Node &n = H.node[i];
        double cap = n.sw ? n.P * 7000.0 : 8192.0 + n.ntp * 1100.0;
        psz[i] = (int32_t)(cap * poolScale);
        ssz[i] = (int32_t)((64 + n.ntp * 80 + n.njetty * 4) * poolScale);
        esz[i] = (int32_t)((64 + n.ntp * 4 + n.njetty * 2) * poolScale);
    }
    mkpool(H.pkt, psz, [](Node &n, int32_t h) { n.pktFree = h; });
    mkpool(H.seg, ssz, [](Node &n, int32_t h) { n.segFree = h; });
    mkpool(H.ent, esz, [](Node &n, int32_t h) { n.entFree = h; });
    // routes (UbUtils::AddRoutingTable + TryGetIndexedCandidatesWithPrimaryFallback; shortest only)
    H.rtOff.assign((size_t)NN * NN * HostWorld::MAXHP, -1);
    H.rtCnt.assign((size_t)NN * NN * HostWorld::MAXHP, 0);
    for (int a = 0; a < NN; ++a)
        for (int b = 0; b < NN; ++b) {
            if (H.node[b].sw)
                continue;
            for (int dp = 0; dp < H.node[b].P && dp < HostWorld::MAXHP; ++dp) {
                std::vector<uint16_t> sh, ot;
                bool foundExact = false;
                auto it = C.exact.find({a, b, dp});
                if (it != C.exact.end()) {
                    sh = it->second.shortest;
                    ot = it->second.other;
                    foundExact = true;
                }
                if (!(foundExact && !sh.empty() && !ot.empty())) {
                    auto jt = C.primary.find({a, b});
                    if (jt != C.primary.end()) {
                        if (sh.empty()) sh = jt->second.shortest;
                        if (ot.empty()) ot = jt->second.other;
                    }
                }
                size_t key = ((size_t)a * NN + b) * HostWorld::MAXHP + dp;
                H.rtOff[key] = (int32_t)H.rtPorts.size();
                H.rtCnt[key] = (int32_t)sh.size();
                for (auto p : sh) H.rtPorts.push_back(p);
            }
        }
    H.w.P = P;
    H.fill_world();
    return "";
}

} // namespace ub
