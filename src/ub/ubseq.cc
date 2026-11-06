// twins-ubseq: a sequential model of ns-3-ub's UnifiedBus data path that reproduces single-threaded
// ns-3-ub event for event. It is the reference the GPU model (twins-ub) is built from: every
// handler below follows one ns-3-ub function, in the same order of side effects and Schedule
// calls, and the event queue orders events like ns-3's MapScheduler, by (time, uid), with uid a
// counter over all Schedule calls the model makes.
//
// Supported subset (anything else stops the run): URMA_WRITE over RTP, one TP per host pair and
// priority, CBFC, the SP round-robin allocator, PER_FLOW shortest-path routing with HASH64, no
// congestion control, no retransmission, no faults. See docs/ub/model.md.
//
// Usage: twins-ubseq CASE_DIR [--out=PREFIX] [--evlog]
//   PREFIX.port : time_ps node port T|R size   (PortTrace order: time, then event order)
//   PREFIX.task : time_ps node task S|C        (WQE Starts / Completes)
#include "ubcase.h"
#include "ubhash.h"
#include "ubtime.h"
#include <cassert>
#include <cmath>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <queue>

using namespace ub;

static void die(const std::string &m)
{
    fprintf(stderr, "twins-ubseq: %s\n", m.c_str());
    exit(2);
}

// ----------------------------------------------------------------------------------------------
// constants of ns-3-ub (ub-datatype.h, ub-cbfc.h, packet header sizes measured on the reference)
constexpr uint32_t MTU = 4096;
constexpr uint32_t SEG_BYTES = 64 * 1024;
constexpr uint32_t HDR_WRITE = 78;   // DL+NET+IPv4+UDP+TP+BTAH+MAETAH of a WRITE request packet
constexpr uint32_t HDR_TAACK = 62;   // a TA response (TAACK) packet, zero payload
constexpr uint32_t SZ_TPACK = 62;    // a TP ACK
constexpr uint32_t SZ_CTRL = 40;     // a CBFC control frame
constexpr int VLN = 16;
constexpr uint32_t EGRESS_MAX = 512 * 1024;   // UbEgressQueue byte limit (ub-port.cc)

enum PktType : uint8_t { P_DATA, P_ACK, P_CTRL };

struct Pkt {
    uint32_t size = 0;
    uint8_t type = P_DATA;
    uint8_t vl = 0;
    bool crd = false;          // piggybacked credit (UbDatalinkPacketHeader credit bit)
    uint8_t crdVl = 0;
    uint32_t sip = 0, dip = 0;
    uint16_t usport = 0, udport = 0;
    int srcTpn = 0, dstTpn = 0;
    uint64_t psn = 0, msn = 0;
    bool last = false;
    // transaction view
    bool resp = false;         // TA response (TAACK) carried as TP data
    int task = -1;
    int jetty = 0;             // IniRcId
    uint32_t taSsn = 0;        // IniTaSsn
    uint32_t payload = 0;
    uint8_t credits[VLN] = {}; // control frame
};

// ----------------------------------------------------------------------------------------------
struct Ev {
    int64_t ts;
    uint64_t uid;
    int node;
    std::function<void()> fn;
};
struct EvCmp {
    bool operator()(const Ev *a, const Ev *b) const
    {
        return a->ts != b->ts ? a->ts > b->ts : a->uid > b->uid;
    }
};

static int64_t g_now = 0;
static uint64_t g_uid = 1;
static int g_ctx = -1;
static bool g_evlog = false;
static std::priority_queue<Ev *, std::vector<Ev *>, EvCmp> g_q;
static uint64_t g_events = 0;

static void schedule(int node, int64_t delay, std::function<void()> fn)
{
    Ev *e = new Ev{g_now + delay, g_uid++, node, std::move(fn)};
    g_q.push(e);
}
static void schedule_now(std::function<void()> fn) { schedule(g_ctx, 0, std::move(fn)); }

// ----------------------------------------------------------------------------------------------
static uint32_t node_ip(int id) { return 0x0a000000u + (id / 256) * 0x10000u + (id % 256) * 0x100u; }
static uint32_t node_port_ip(int id, int port)
{
    return 0x0a000000u + (id / 256) * 0x10000u + (id % 256) * 0x100u + (uint32_t)((port + 1) % 256);
}
static int ip_node(uint32_t ip)
{
    ip -= 0x0a000000u;
    return (int)(((ip & 0x00ff0000u) >> 16) * 256 + ((ip & 0xff00u) >> 8));
}

struct Node;
struct Tp;

// An ingress queue of the allocator: a VOQ (in port's packets for one out port and VL) or a TP.
struct IQ {
    bool isTp = false;
    int inPort = 0;          // VOQ
    std::deque<int> pkts;    // VOQ packet ids
    Tp *tp = nullptr;
    bool control = false;    // VOQ of locally generated control frames (in == out, VL 0)
};

struct Port {
    int peerNode = -1, peerPort = -1;
    uint64_t bps = 0;
    int64_t delay = 0;
    bool busy = false;
    std::deque<int> egress;
    uint32_t egressBytes = 0;
    // CBFC (UbCbfc)
    int32_t txfree[VLN];
    int32_t toReturn[VLN] = {};
    uint8_t lastPiggyVl = 0;
    // allocator (UbRoundRobinAllocator) state of this output port
    bool running = false, runAgain = false, allocPending = false;
    IQ *active = nullptr;
    uint32_t rrIdx[VLN] = {};
    bool rrSeeded[VLN] = {};
    uint32_t selectedSlot = 0;
    // ingress sources by VL: VOQs (by in port) then TPs in registration order
    std::vector<IQ *> src[VLN];
    std::vector<int64_t> txt;    // tx time by size (filled on demand)
};

struct Seg {
    int task = -1, jetty = 0;
    bool resp = false;
    uint64_t size = 0;            // carrier bytes
    uint64_t bytesLeft = 0;
    uint32_t payloadPerPkt = 0;   // 0 for zero-payload responses
    uint64_t psnStart = 0;
    uint32_t psnSize = 0;
    uint64_t msn = 0;
    uint32_t taSsn = 0;           // request TA SSN (responses carry the request's)
    uint32_t wireHdr = 0;
};

struct Op;   // a WQE operation (UbTransaction::OperationRecord + the WQE)

struct TxEntry {
    bool remote = false;
    Op *op = nullptr;            // local
    int rkey = -1;               // remote segments key (origin jetty, task)
};

struct Conn;
struct Tp {
    Node *node = nullptr;
    Conn *conn = nullptr;
    int slot = 1;                 // 1: the connection owner's endpoint, 2: the peer's
    bool draining = false, drainHold = false, frozen = false, dead = false;
    int workLeases = 0;           // admitted request segments awaiting their TA response
    int tpn = 0, peer = 0, peerTpn = 0, port = 0, dport = 0, pri = 0;
    uint32_t sip = 0, dip = 0;
    IQ iq;
    // sender (UbTransportChannel)
    uint64_t psnSndNxt = 0, psnSndUna = 0, tpPsnCnt = 0, msnCnt = 0;
    std::deque<Seg *> segs;
    std::deque<int> ackQ;
    bool sendWindowLimited = false;
    // receiver
    uint64_t psnRecvNxt = 0;
    std::map<uint64_t, int> buffered;   // psn -> pkt (out of order)
    struct InUnit {
        uint64_t bytes = 0;
    };
    std::map<std::pair<int, uint64_t>, InUnit> inbound;
    // RtpTxQueue of this TP (UbTransaction::m_rtpTxQueues[DIRECT_TP])
    std::deque<TxEntry> pending;
    bool txEventPending = false, txEntryActive = false, waiting = false;
    TxEntry activeEntry;
    // remote (response) segments by (origin jetty, task)
    std::map<std::pair<int, int>, std::pair<std::deque<Seg *>, bool>> remote;
};

struct Op {
    int task = -1, jetty = 0;
    Tp *tp = nullptr;
    uint64_t size = 0, bytesLeft = 0;
    bool canSend = false, dispatchReady = false, txEntryQueued = false, segTxActive = false;
    std::map<uint32_t, Seg *> segments;   // admitted, awaiting TA response, by TA SSN
    bool done = false;
};

struct Jetty {
    bool bound = true;            // m_jettyRtpReferences entry (released by task cleanup)
    int num = 0;
    Op *op = nullptr;
    uint32_t taSsnSndNxt = 0, taSsnSndUna = 0;
    std::set<uint32_t> acked;
    Tp *tp = nullptr;
};

struct Node {
    int id = 0;
    bool sw = false;
    int64_t alloc = 0, fwd = 0;
    std::vector<Port> ports;
    std::map<std::tuple<int, int, int>, IQ *> voq;   // (out, vl, in)
    // queue manager: ingress bytes by (in port, vl) (admission = reserve per queue)
    std::vector<std::array<uint64_t, VLN>> ingressBytes;
    // host side
    std::map<int, Tp *> tps;          // by local tpn
    std::map<int, Jetty *> jetties;   // by jetty number
    int nextJetty = 0;
    int nextTpn = 0;
    std::vector<int> nTpsByPortVl;    // [port*VLN + vl]
};

// ----------------------------------------------------------------------------------------------
static Case C;
static std::vector<Node> N;
static std::vector<Pkt> P;     // packet pool (never freed; runs are short)
static std::deque<int> freeP;
static uint64_t reservePerQueue = 1048576;
static int vlNum = 16;
static int32_t cbfcInit = 6553, cbfcThld = 1024;
static int cbfcGrainData = 4, cbfcGrainCtrl = 32;
static int cbfcCellBytes = 160;
static int64_t depDelay = 100000;
static uint64_t inflightMax = 1000, wqeSegMax = 1000;
static FILE *fport = nullptr, *ftask = nullptr;
static uint64_t drops = 0;

static int newpkt()
{
    if (!freeP.empty()) {
        int i = freeP.front();
        freeP.pop_front();
        P[i] = Pkt();
        return i;
    }
    P.emplace_back();
    return (int)P.size() - 1;
}
static void freepkt(int i) { freeP.push_back(i); }

static int64_t txtime(Port &pt, uint32_t bytes)
{
    if (bytes >= pt.txt.size()) {
        size_t old = pt.txt.size();
        pt.txt.resize(bytes + 1);
        for (size_t b = old; b <= bytes; ++b)
            pt.txt[b] = tx_ps(pt.bps, (uint32_t)b);
    }
    return pt.txt[bytes];
}

static int32_t cells(uint32_t size) { return (int32_t)ceil((float)size / (float)cbfcCellBytes); }

// ----------------------------------------------------------------------------------------------
// traffic gen state
struct TaskState {
    int state = 0;   // 0 pending, 1 ready, 2 running, 3 completed
    int pendingPhases = 0;
    int jetty = -1;
};
static std::vector<TaskState> TS;
static std::map<int, int> taskIdx;   // taskId -> index in C.tasks
struct PhaseState {
    int total = 0, completed = 0;
    std::vector<int> dependents;
};
static std::map<int, PhaseState> phases;
static std::vector<int> readyTasks;
static int completedTasks = 0;

static void start_task(int ti);
static void tp_notify(Tp *tp);
static void port_trigger(Node &n, int p);
static void trigger_allocator(Node &n, int p);
static void try_continue_jetty_tp(Tp *tp);
static void enqueue_rtp_wqe(Op *op, bool front);
static void run_rtp_tx(Tp *tp);
static void schedule_rtp_tx(Tp *tp);

// ----------------------------------------------------------------------------------------------
// TP as an allocator ingress queue (UbTransportChannel::IsEmpty / IsLimited / GetNextPacketSize)
static bool tp_has_work(Tp *tp)
{
    if (!tp->ackQ.empty())
        return true;
    if (tp->segs.empty())
        return false;
    return tp->psnSndNxt < tp->tpPsnCnt;
}
static bool tp_inflight_limited(Tp *tp) { return tp->psnSndNxt - tp->psnSndUna >= inflightMax; }
static int tp_active_segments(Tp *tp)
{
    int c = 0;
    for (Seg *s : tp->segs)
        c += s->bytesLeft != 0;
    return c;
}
static bool tp_can_schedule_another(Tp *tp) { return tp_active_segments(tp) < 2; }
static bool tp_seg_limited(Tp *tp) { return tp->segs.size() >= wqeSegMax; }

static bool iq_empty(IQ *q) { return q->isTp ? !tp_has_work(q->tp) : q->pkts.empty(); }
static bool iq_limited(IQ *q)
{
    if (!q->isTp)
        return false;
    Tp *tp = q->tp;
    if (!tp->ackQ.empty())
        return false;
    if (tp_inflight_limited(tp)) {
        tp->sendWindowLimited = true;
        return true;
    }
    return false;
}
static uint32_t seg_next_pkt_size(Seg *s)
{
    uint32_t progress = (uint32_t)std::min<uint64_t>(s->bytesLeft, MTU);
    uint32_t payload = s->payloadPerPkt ? progress : 0;
    return payload + s->wireHdr;
}
static uint32_t iq_next_size(IQ *q)
{
    if (!q->isTp)
        return P[q->pkts.front()].size;
    Tp *tp = q->tp;
    if (!tp->ackQ.empty())
        return P[tp->ackQ.front()].size;
    for (Seg *s : tp->segs)
        if (s->bytesLeft != 0)
            return seg_next_pkt_size(s);
    return 0;
}

// TryGetNextNewDataPacket + SendNewDataPacket + AdvanceNewDataSendState
static int tp_next_packet(Tp *tp)
{
    if (!tp->ackQ.empty()) {
        int k = tp->ackQ.front();
        tp->ackQ.pop_front();
        tp_notify(tp);
        return k;
    }
    if (tp->segs.empty())
        return -1;
    if (tp_inflight_limited(tp)) {
        tp->sendWindowLimited = true;
        return -1;
    }
    Seg *s = nullptr;
    for (Seg *x : tp->segs)
        if (x->bytesLeft != 0) {
            s = x;
            break;
        }
    if (!s)
        return -1;
    uint32_t progress = (uint32_t)std::min<uint64_t>(s->bytesLeft, MTU);
    uint32_t payload = s->payloadPerPkt ? progress : 0;
    int k = newpkt();
    Pkt &p = P[k];
    p.type = P_DATA;
    p.size = payload + s->wireHdr;
    p.vl = (uint8_t)tp->pri;
    p.sip = tp->sip;
    p.dip = tp->dip;
    p.usport = 0;                   // m_lbHashSalt: 0 for per-flow routing
    p.udport = (uint16_t)tp->dport;
    p.srcTpn = tp->tpn;
    p.dstTpn = tp->peerTpn;
    p.psn = tp->psnSndNxt;
    p.msn = s->msn;
    p.last = s->bytesLeft == progress;
    p.resp = s->resp;
    p.task = s->task;
    p.jetty = s->jetty;
    p.taSsn = s->taSsn;
    p.payload = payload;
    // AdvanceNewDataSendState
    s->bytesLeft -= progress;
    tp->psnSndNxt++;
    if (s->bytesLeft == 0 && tp_can_schedule_another(tp))
        try_continue_jetty_tp(tp);
    return k;
}

// ----------------------------------------------------------------------------------------------
// CBFC (UbCbfc)
static bool fc_limited(Port &out, IQ *q)
{
    if (!q->isTp && q->control)
        return false;
    uint32_t sz = iq_next_size(q);
    int vl = q->isTp ? q->tp->pri : (int)P[q->pkts.front()].vl;
    return out.txfree[vl] < cells(sz);
}

static void send_control_frame(Node &n, int p, int k);

// BuildControlReturnPacket loop of MaybeQueueControlReturn
static void maybe_queue_control_return(Node &n, int p)
{
    Port &pt = n.ports[p];
    auto force = [&]() {
        for (int v = 0; v < VLN; ++v)
            if (pt.toReturn[v] >= cbfcThld)
                return true;
        return false;
    };
    if (!force())
        return;
    for (;;) {
        uint8_t cr[VLN] = {};
        bool any = false;
        for (int v = 0; v < vlNum; ++v) {
            int32_t left = pt.toReturn[v];
            if (left >= cbfcGrainCtrl) {
                int32_t g = std::min<int32_t>(left / cbfcGrainCtrl, 63);
                if (g > 0) {
                    cr[v] = (uint8_t)g;
                    if (pt.toReturn[v] >= g * cbfcGrainCtrl)
                        pt.toReturn[v] -= g * cbfcGrainCtrl;
                    any = true;
                }
            }
        }
        if (!any)
            break;
        int k = newpkt();
        P[k].type = P_CTRL;
        P[k].size = SZ_CTRL;
        P[k].vl = 0;
        memcpy(P[k].credits, cr, sizeof cr);
        send_control_frame(n, p, k);
    }
}
static void accumulate_returned_credit(Node &n, int p, int k)
{
    Pkt &pk = P[k];
    n.ports[p].toReturn[pk.vl] += cells(pk.size);
}
// OnEgressEnqueued: consume, then piggyback a return for the next VL in round-robin order
static void fc_on_egress_enqueued(Port &pt, IQ *q, int k)
{
    if (!q->isTp && q->control)
        return;
    Pkt &pk = P[k];
    int32_t c = cells(pk.size);
    if (pt.txfree[pk.vl] >= c)
        pt.txfree[pk.vl] -= c;
    if (pk.crd)
        return;
    for (int off = 1; off <= vlNum; ++off) {
        uint8_t cand = (uint8_t)((pt.lastPiggyVl + off) % vlNum);
        if (pt.toReturn[cand] >= cbfcGrainData) {
            pk.crd = true;
            pk.crdVl = cand;
            pt.toReturn[cand] -= cbfcGrainData;
            pt.lastPiggyVl = cand;
            return;
        }
    }
}

// ----------------------------------------------------------------------------------------------
// allocator
static IQ *select_next(Node &n, int p)
{
    Port &pt = n.ports[p];
    int P0 = (int)n.ports.size();
    for (int pi = 0; pi < vlNum; ++pi) {
        uint32_t slots = P0 + n.nTpsByPortVl[p * VLN + pi];
        if (slots == 0)
            continue;
        if (!pt.rrSeeded[pi]) {
            pt.rrIdx[pi] = (uint32_t)((n.id + p) % slots);
            pt.rrSeeded[pi] = true;
        } else if (pt.rrIdx[pi] >= slots)
            pt.rrIdx[pi] %= slots;
        auto &qs = pt.src[pi];
        if (qs.empty())
            continue;
        uint32_t start = pt.rrIdx[pi];
        IQ *best = nullptr, *wrap = nullptr;
        uint32_t bs = 0, ws = 0;
        uint32_t ord = 0;
        for (IQ *q : qs) {
            uint32_t slot = q->isTp ? P0 + ord++ : (uint32_t)q->inPort;
            if (slot >= slots || iq_empty(q) || iq_limited(q))
                continue;
            if (fc_limited(pt, q))
                continue;
            if (slot >= start) {
                if (!best || slot < bs) best = q, bs = slot;
            } else if (!wrap || slot < ws)
                wrap = q, ws = slot;
        }
        IQ *c = best ? best : wrap;
        uint32_t cs = best ? bs : ws;
        if (getenv("UB_DBG_NODE") && atoi(getenv("UB_DBG_NODE")) == n.id) {
            fprintf(stderr, "%lld sel n%d p%d vl%d start %u ->", (long long)g_now, n.id, p, pi, start);
            uint32_t o2 = 0;
            for (IQ *q : qs) {
                uint32_t slot = q->isTp ? P0 + o2++ : (uint32_t)q->inPort;
                if (q->isTp)
                    fprintf(stderr, " [s%u tpn%d e%d l%d]", slot, q->tp->tpn, iq_empty(q), iq_limited(q));
                else
                    fprintf(stderr, " [s%u voq e%d]", slot, iq_empty(q));
            }
            fprintf(stderr, " chosen %d\n", c ? (int)cs : -1);
        }
        if (c) {
            pt.selectedSlot = cs;
            pt.rrIdx[pi] = (cs + 1) % slots;
            return c;
        }
    }
    return nullptr;
}

static bool has_nonempty_ingress(Node &n, int p)
{
    Port &pt = n.ports[p];
    for (int pi = 0; pi < vlNum; ++pi)
        for (IQ *q : pt.src[pi])
            if (!iq_empty(q))
                return true;
    return false;
}

static void dequeue_packet(Node &n, int p);
static void notify_allocation_finish(Node &n, int p)
{
    Port &pt = n.ports[p];
    if (pt.busy || pt.egress.empty())
        return;
    dequeue_packet(n, p);
}

static void allocate_next(Node &n, int p)
{
    Port &pt = n.ports[p];
    pt.allocPending = false;
    bool owned = pt.running;
    IQ *q = pt.active;
    if (!owned && !q)
        die("allocation without an episode");
    if (q) {
        uint32_t nsz = iq_next_size(q);
        if (pt.egressBytes + nsz > EGRESS_MAX) {
            int pri = q->isTp ? q->tp->pri : P[q->pkts.front()].vl;
            pt.rrIdx[pri] = pt.selectedSlot;
            pt.active = nullptr;
            pt.running = false;
            return;
        }
        int k;
        if (q->isTp) {
            k = tp_next_packet(q->tp);
            if (k < 0)
                die("TP returned no packet at allocation commit (ns-3-ub would crash here)");
        } else {
            k = q->pkts.front();
            q->pkts.pop_front();
        }
        pt.egress.push_back(k);
        pt.egressBytes += P[k].size;
        fc_on_egress_enqueued(pt, q, k);
        if (!q->isTp && !q->control) {
            // NotifySwitchDequeue: PopFromVoq; in port's OnIngressReleased returns credit
            int in = q->inPort;
            n.ingressBytes[in][P[k].vl] -= P[k].size;
            if (in != p) {
                accumulate_returned_credit(n, in, k);
                maybe_queue_control_return(n, in);
            }
        }
    }
    pt.active = nullptr;
    if (!pt.egress.empty())
        notify_allocation_finish(n, p);
    bool again = owned && pt.runAgain && has_nonempty_ingress(n, p);
    pt.runAgain = false;
    pt.running = false;
    if (again)
        trigger_allocator(n, p);
}

static void trigger_allocator(Node &n, int p)
{
    Port &pt = n.ports[p];
    if (pt.running) {
        pt.runAgain = true;
        return;
    }
    pt.running = true;
    pt.runAgain = false;
    pt.active = select_next(n, p);
    if (pt.allocPending)
        die("two pending allocations");
    pt.allocPending = true;
    Node *np = &n;
    schedule(n.id, n.alloc, [np, p]() { allocate_next(*np, p); });
}

// ----------------------------------------------------------------------------------------------
// port
static void receive(Node &n, int p, int k);
static void transmit_packet(Node &n, int p, int k)
{
    Port &pt = n.ports[p];
    Pkt &pk = P[k];
    if (fport)
        fprintf(fport, "%lld %d %d T %u %d %d %llu %d\n", (long long)g_now, n.id, p, pk.size, pk.type, pk.task,
                (unsigned long long)pk.psn, pk.srcTpn);
    int64_t tx = txtime(pt, pk.size);
    Node *peer = &N[pt.peerNode];
    int pp = pt.peerPort;
    schedule(peer->id, tx + pt.delay, [peer, pp, k]() { receive(*peer, pp, k); });
    Node *np = &n;
    schedule(n.id, tx, [np, p]() {
        np->ports[p].busy = false;
        schedule_now([np, p]() { port_trigger(*np, p); });
    });
}
static void dequeue_packet(Node &n, int p)
{
    Port &pt = n.ports[p];
    pt.busy = true;
    int k = pt.egress.front();
    pt.egress.pop_front();
    pt.egressBytes -= P[k].size;
    if (pt.egress.empty())
        trigger_allocator(n, p);
    transmit_packet(n, p, k);
}
static void port_trigger(Node &n, int p)
{
    Port &pt = n.ports[p];
    if (pt.busy) {
        if (pt.egress.empty())
            trigger_allocator(n, p);
        return;
    }
    if (pt.egress.empty()) {
        trigger_allocator(n, p);
        return;
    }
    dequeue_packet(n, p);
}

static IQ *get_voq(Node &n, int out, int vl, int in)
{
    auto key = std::make_tuple(out, vl, in);
    auto it = n.voq.find(key);
    if (it != n.voq.end())
        return it->second;
    IQ *q = new IQ;
    q->inPort = in;
    q->control = in == out;
    auto &qs = n.ports[out].src[vl];
    // VOQs sorted by in port, ahead of the TPs
    auto at = qs.begin();
    for (; at != qs.end(); ++at)
        if ((*at)->isTp || (*at)->inPort > in)
            break;
    qs.insert(at, q);
    n.voq[key] = q;
    return q;
}
static void send_control_frame(Node &n, int p, int k)
{
    get_voq(n, p, 0, p)->pkts.push_back(k);
    port_trigger(n, p);
}

// ----------------------------------------------------------------------------------------------
// routing (UbRoutingProcess, HASH64, per-flow shortest paths)
static int route(Node &n, Pkt &pk, int inPort)
{
    int dn = ip_node(pk.dip);
    uint32_t last = pk.dip & 0xff;
    std::vector<uint16_t> sh, ot;
    bool foundExact = false;
    if (last != 0) {
        auto it = C.exact.find({n.id, dn, (int)last - 1});
        if (it != C.exact.end()) {
            sh = it->second.shortest;
            ot = it->second.other;
            foundExact = true;
        }
    }
    if (last == 0 || !(foundExact && !sh.empty() && !ot.empty())) {
        auto it = C.primary.find({n.id, dn});
        if (it != C.primary.end()) {
            if (sh.empty()) sh = it->second.shortest;
            if (ot.empty()) ot = it->second.other;
        }
    }
    auto rm = [&](std::vector<uint16_t> &v) { v.erase(std::remove(v.begin(), v.end(), inPort), v.end()); };
    rm(sh);
    rm(ot);
    ot.clear();   // shortest-path routing type
    if (sh.empty())
        die("no route at node " + std::to_string(n.id) + " to " + std::to_string(dn));
    uint8_t b[17];
    uint32_t salt = node_ip(n.id);
    b[0] = pk.sip >> 24; b[1] = pk.sip >> 16; b[2] = pk.sip >> 8; b[3] = pk.sip;
    b[4] = pk.dip >> 24; b[5] = pk.dip >> 16; b[6] = pk.dip >> 8; b[7] = pk.dip;
    b[8] = pk.usport >> 8; b[9] = pk.usport; b[10] = pk.udport >> 8; b[11] = pk.udport;
    b[12] = pk.vl;
    b[13] = salt >> 24; b[14] = salt >> 16; b[15] = salt >> 8; b[16] = salt;
    uint64_t h = ns3_hash64(b, 17);
    return sh[h % sh.size()];
}

static void forward(Node &n, int in, int k)
{
    Pkt &pk = P[k];
    int out = route(n, pk, in);
    if (pk.vl == 0)
        die("data packet on VL 0");
    // CheckInPortSpace: reserve per (in port, VL); shared pool and headroom are 0 by default
    if (n.ingressBytes[in][pk.vl] + pk.size > reservePerQueue) {
        ++drops;
        die("ingress buffer full (drop) at node " + std::to_string(n.id));
    }
    n.ingressBytes[in][pk.vl] += pk.size;
    auto finish = [&n, in, out, k]() {
        get_voq(n, out, P[k].vl, in)->pkts.push_back(k);
        port_trigger(n, out);
    };
    if (n.fwd <= 0)
        finish();
    else {
        Node *np = &n;
        schedule(n.id, n.fwd, [np, in, out, k]() {
            get_voq(*np, out, P[k].vl, in)->pkts.push_back(k);
            port_trigger(*np, out);
        });
    }
}

// ----------------------------------------------------------------------------------------------
// TP creation (UbTransportChannelManager::CreateTp -> UbSwitch::RegisterTpWithAllocator)
struct Conn {
    int n1, p1, tpn1, n2, p2, tpn2, pri;
    int uses = 0;                 // planned uses (tasks of both directions) not yet completed
    Tp *ep[3] = {nullptr, nullptr, nullptr};   // materialized endpoints by slot
    bool destroyRequested = false;
    int retired[3] = {0, 0, 0};
};
static std::map<std::tuple<int, int, int>, Conn *> conns;   // (local node, peer, pri) -> conn

struct Conn;
static Tp *create_tp(Node &n, int peer, int port, int dport, int pri, int tpn, int peerTpn)
{
    Tp *tp = new Tp;
    tp->node = &n;
    tp->tpn = tpn;
    tp->peer = peer;
    tp->peerTpn = peerTpn;
    tp->port = port;
    tp->dport = dport;
    tp->pri = pri;
    tp->sip = node_port_ip(n.id, port);
    tp->dip = node_port_ip(peer, dport);
    tp->iq.isTp = true;
    tp->iq.tp = tp;
    n.tps[tpn] = tp;
    n.ports[port].src[pri].push_back(&tp->iq);
    n.nTpsByPortVl[port * VLN + pri]++;
    return tp;
}
static Tp *local_tp(Node &n, int peer, int pri)
{
    auto it = conns.find({n.id, peer, pri});
    if (it == conns.end())
        die("no reserved connection");
    Conn &c = *it->second;
    bool first = c.n1 == n.id;
    int tpn = first ? c.tpn1 : c.tpn2;
    auto t = n.tps.find(tpn);
    if (t != n.tps.end())
        return t->second;
    if (c.destroyRequested)
        die("endpoint materialization after its connection was destroyed");
    Tp *tp = first ? create_tp(n, c.n2, c.p1, c.p2, pri, c.tpn1, c.tpn2)
                   : create_tp(n, c.n1, c.p2, c.p1, pri, c.tpn2, c.tpn1);
    tp->conn = &c;
    tp->slot = first ? 1 : 2;
    c.ep[tp->slot] = tp;
    return tp;
}

// ----------------------------------------------------------------------------------------------
// connection teardown (TpConnectionManager::DestroyConnectionOwned and the endpoint drain of
// UbTransportChannelManager): every control record is applied synchronously in a sequential run
static bool tp_quiescent(Tp *tp)
{
    if (!tp->ackQ.empty() || !tp->segs.empty() || tp->psnSndUna != tp->psnSndNxt)
        return false;
    if (!tp->inbound.empty() || !tp->buffered.empty() || tp->workLeases)
        return false;
    if (!tp->pending.empty() || tp->txEntryActive || !tp->remote.empty())
        return false;
    for (auto &[num, j] : tp->node->jetties)
        if (j->bound && j->tp == tp)
            return false;
    return true;
}
static void destroy_endpoint(Conn *c, int slot);
static void endpoint_destroyed(Conn *c, int slot)
{
    c->retired[slot] = 1;
    if (slot == 1)
        destroy_endpoint(c, 2);
}
static void finalize_tp(Tp *tp)
{
    Node &n = *tp->node;
    auto &qs = n.ports[tp->port].src[tp->pri];
    qs.erase(std::remove(qs.begin(), qs.end(), &tp->iq), qs.end());
    n.nTpsByPortVl[tp->port * VLN + tp->pri]--;
    n.tps.erase(tp->tpn);
    tp->dead = true;
    if (getenv("UB_DBG_TP"))
        fprintf(stderr, "%lld destroy n%d tpn%d\n", (long long)g_now, n.id, tp->tpn);
    endpoint_destroyed(tp->conn, tp->slot);
}
static void tp_notify(Tp *tp)
{
    if (tp->dead || !tp->draining || tp->drainHold || !tp_quiescent(tp))
        return;
    finalize_tp(tp);
}
// DestroyTp(tpn, reason, callback, releaseConnectionDrainHold = true)
static void destroy_endpoint(Conn *c, int slot)
{
    Tp *tp = c->ep[slot];
    if (!tp || tp->dead) {
        endpoint_destroyed(c, slot);   // NOT_FOUND: never materialized
        return;
    }
    tp->drainHold = false;
    tp->draining = true;
    tp->frozen = true;
    tp_notify(tp);
}
static void destroy_connection(Conn *c)
{
    if (c->destroyRequested)
        return;
    c->destroyRequested = true;
    if (c->ep[2] && !c->ep[2]->dead) {   // freeze notice: FreezeTp holds the peer's drain
        c->ep[2]->drainHold = true;
        c->ep[2]->frozen = true;
    }
    destroy_endpoint(c, 1);
}

// ----------------------------------------------------------------------------------------------
// transaction (UbTransaction RTP queue)
static Op *op_of(TxEntry &e) { return e.op; }

static void finish_active(Op *op)
{
    Tp *tp = op->tp;
    if (tp->txEntryActive && !tp->activeEntry.remote && tp->activeEntry.op == op) {
        tp->txEntryActive = false;
        tp->activeEntry = TxEntry();
    }
}
static void schedule_rtp_tx(Tp *tp)
{
    if (tp->waiting || tp->pending.empty() || tp->txEventPending || tp->txEntryActive)
        return;
    tp->txEventPending = true;
    schedule_now([tp]() { run_rtp_tx(tp); });
}
static void try_continue_rtp_queue(Tp *tp)
{
    if (!tp->waiting || tp->pending.empty() || tp->txEventPending)
        return;
    tp->waiting = false;
    schedule_rtp_tx(tp);
}

static void admit_segment(Op *op, Seg *s);
static void prepare_segment(Op *op)
{
    if (op->done)
        die("prepare for a finished operation");
    Jetty *j = op->tp->node->jetties[op->jetty];
    Seg *s = nullptr;
    if (op->bytesLeft != 0) {
        s = new Seg;
        s->task = op->task;
        s->jetty = op->jetty;
        s->size = std::min<uint64_t>(op->bytesLeft, SEG_BYTES);
        s->bytesLeft = s->size;
        s->payloadPerPkt = 1;
        s->psnSize = (uint32_t)((s->size + MTU - 1) / MTU);
        s->taSsn = j->taSsnSndNxt;
        s->wireHdr = HDR_WRITE;
    }
    if (s)
        schedule_now([op, s]() { admit_segment(op, s); });
    else {
        op->segTxActive = false;
        finish_active(op);
        schedule_rtp_tx(op->tp);
    }
}
static void start_segment_tx(Op *op)
{
    if (op->done || !op->dispatchReady || op->segTxActive)
        return;
    op->segTxActive = true;
    schedule_now([op]() { prepare_segment(op); });
}
static std::map<int, std::pair<int, int>> rkeys;   // rkey -> (origin jetty, task)
static void run_remote_key(Tp *tp, std::pair<int, int> key);
static void run_rtp_tx(Tp *tp)
{
    tp->txEventPending = false;
    if (tp->txEntryActive || tp->waiting)
        return;
    while (!tp->pending.empty()) {
        TxEntry e = tp->pending.front();
        tp->pending.pop_front();
        if (e.remote) {
            run_remote_key(tp, rkeys[e.rkey]);
            break;
        }
        Op *op = e.op;
        if (op->done)
            continue;
        op->txEntryQueued = false;
        tp->txEntryActive = true;
        tp->activeEntry = e;
        start_segment_tx(op);
        break;
    }
}
static void enqueue_rtp_wqe(Op *op, bool front)
{
    if (op->done || op->segTxActive)
        return;
    Tp *tp = op->tp;
    if (!op->txEntryQueued) {
        TxEntry e;
        e.op = op;
        if (front)
            tp->pending.push_front(e);
        else
            tp->pending.push_back(e);
        op->txEntryQueued = true;
    }
    if (!front && !tp->txEntryActive && !tp->txEventPending && !tp->waiting)
        run_rtp_tx(tp);
}
static bool tp_admit(Tp *tp, Seg *s)
{
    if (tp_seg_limited(tp) || !tp_can_schedule_another(tp))
        return false;
    s->msn = tp->msnCnt;
    s->psnStart = tp->tpPsnCnt;
    tp->tpPsnCnt += s->psnSize;
    tp->msnCnt++;
    tp->segs.push_back(s);
    return true;
}
static void admit_segment(Op *op, Seg *s)
{
    Tp *tp = op->tp;
    Jetty *j = tp->node->jetties[op->jetty];
    int result;   // 0 admitted, 1 stale, 2 deferred
    if (s->taSsn != j->taSsnSndNxt || op->bytesLeft < s->size)
        result = 1;
    else if (tp_seg_limited(tp) || !tp_can_schedule_another(tp))
        result = 2;
    else {
        op->segments[s->taSsn] = s;
        tp_admit(tp, s);
        tp->workLeases++;
        op->canSend = true;
        op->bytesLeft -= s->size;
        j->taSsnSndNxt++;
        result = 0;
    }
    op->segTxActive = false;
    finish_active(op);
    if (result == 0) {
        port_trigger(*tp->node, tp->port);
        if (op->bytesLeft != 0)
            enqueue_rtp_wqe(op, false);   // TriggerScheduleWqeSegment -> StartDispatchGate
        else
            schedule_rtp_tx(tp);
    } else if (result == 1) {
        delete s;
        enqueue_rtp_wqe(op, false);
    } else {
        delete s;
        enqueue_rtp_wqe(op, true);
        tp->waiting = true;
    }
}

// remote (response) segments: keyed by (origin jetty, task)
static void run_remote_key(Tp *tp, std::pair<int, int> key)
{
    auto it = tp->remote.find(key);
    if (it == tp->remote.end())
        return;
    auto &dq = it->second.first;
    it->second.second = false;
    if (dq.empty()) {
        tp->remote.erase(it);
        return;
    }
    Seg *s = dq.front();
    if (!tp_admit(tp, s)) {
        TxEntry e;
        e.remote = true;
        e.rkey = key.first * 1000003 + key.second;
        tp->pending.push_front(e);
        it->second.second = true;
        tp->waiting = true;
        return;
    }
    dq.pop_front();
    if (!dq.empty()) {
        TxEntry e;
        e.remote = true;
        e.rkey = key.first * 1000003 + key.second;
        tp->pending.push_back(e);
        it->second.second = true;
    } else
        tp->remote.erase(it);
    port_trigger(*tp->node, tp->port);
    schedule_rtp_tx(tp);
}
static void enqueue_remote(Tp *tp, Seg *s)
{
    std::pair<int, int> key{s->jetty, s->task};
    int rk = key.first * 1000003 + key.second;
    rkeys[rk] = key;
    auto &st = tp->remote[key];
    st.first.push_back(s);
    if (!st.second) {
        TxEntry e;
        e.remote = true;
        e.rkey = rk;
        tp->pending.push_back(e);
        st.second = true;
    }
    if (!tp->txEntryActive && !tp->txEventPending && !tp->waiting)
        run_rtp_tx(tp);
}

static void try_continue_jetty(Node &n, int jettyNum)
{
    Jetty *j = n.jetties[jettyNum];
    Op *op = j->op;
    if (!op || op->done || !op->dispatchReady || op->bytesLeft == 0)
        return;
    enqueue_rtp_wqe(op, false);
}
static void try_continue_jetty_tp(Tp *tp)
{
    if (tp_seg_limited(tp) || !tp_can_schedule_another(tp))
        return;
    try_continue_rtp_queue(tp);
    Node &n = *tp->node;
    for (auto &[num, j] : n.jetties)
        if (j->bound && j->tp == tp)
            try_continue_jetty(n, num);
}

// ----------------------------------------------------------------------------------------------
// task completion and the traffic generator
static void apply_task_completion(int ti);
static void task_completed(Node &n, int ti)
{
    TaskCfg &t = C.tasks[ti];
    if (ftask)
        fprintf(ftask, "%lld %d %d C\n", (long long)g_now, n.id, t.id);
    if (TS[ti].state != 2)
        return;
    schedule(n.id, depDelay, [ti]() { apply_task_completion(ti); });
}
static void schedule_tasks(std::vector<int> &batch)
{
    for (int ti : batch) {
        TaskCfg &t = C.tasks[ti];
        schedule(t.src, t.delay_ps, [ti]() { start_task(ti); });
    }
}
static std::vector<int> collect_ready()
{
    std::sort(readyTasks.begin(), readyTasks.end(), [](int a, int b) { return C.tasks[a].id < C.tasks[b].id; });
    readyTasks.erase(std::unique(readyTasks.begin(), readyTasks.end()), readyTasks.end());
    std::vector<int> batch;
    for (int ti : readyTasks)
        if (TS[ti].state == 1) {
            TS[ti].state = 2;
            batch.push_back(ti);
        }
    readyTasks.clear();
    return batch;
}
static void apply_task_completion(int ti)
{
    if (TS[ti].state == 3)
        return;
    TS[ti].state = 3;
    ++completedTasks;
    auto ph = phases.find(C.tasks[ti].phase);
    if (ph != phases.end()) {
        ph->second.completed++;
        if (ph->second.completed >= ph->second.total) {
            for (int d : ph->second.dependents) {
                if (TS[d].state != 0)
                    continue;
                if (--TS[d].pendingPhases == 0) {
                    TS[d].state = 1;
                    readyTasks.push_back(d);
                }
            }
            ph->second.dependents.clear();
        }
    }
    auto b = collect_ready();
    schedule_tasks(b);
    // cleanup request of a completed URMA task (EnableTaskScopedJettyDestroy and
    // EnablePlannedTpConnectionDestroy are on by default): delivered to the task's source node
    TaskCfg &t = C.tasks[ti];
    if (t.op == "URMA_WRITE" || t.op == "URMA_READ") {
        Conn *c = conns.at({t.src, t.dst, t.pri});
        bool destroy = --c->uses == 0;
        int src = t.src, jn = TS[ti].jetty;
        schedule(src, 0, [src, jn, c, destroy]() {
            Node &n = N[src];
            Jetty *j = n.jetties.at(jn);
            j->bound = false;              // ReleaseCompletedJettyTransportBinding
            tp_notify(j->tp);              // NotifyStateChanged(tpn)
            if (destroy)
                destroy_connection(c);
        });
    }
}

static void process_ta_response(Node &n, Tp *tp, int jettyNum, int task, uint32_t taSsn)
{
    auto jt = n.jetties.find(jettyNum);
    if (jt == n.jetties.end())
        die("TA response for unknown jetty");
    Jetty *j = jt->second;
    Op *op = j->op;
    if (!op || op->task != task)
        die("TA response for unknown operation");
    auto sit = op->segments.find(taSsn);
    if (sit == op->segments.end())
        die("TA response for unknown segment");
    op->segments.erase(sit);
    // RecordSegmentSuccess releases the segment's transport-work lease on its TP
    op->tp->workLeases--;
    tp_notify(op->tp);
    // UbJetty::ProcessWqeSegmentComplete
    if (taSsn < j->taSsnSndUna || taSsn >= j->taSsnSndNxt)
        die("TA SSN out of window");
    j->acked.insert(taSsn);
    uint32_t old = j->taSsnSndUna;
    while (j->taSsnSndUna < j->taSsnSndNxt && j->acked.count(j->taSsnSndUna)) {
        j->acked.erase(j->taSsnSndUna);
        j->taSsnSndUna++;
    }
    if (j->taSsnSndUna > old) {
        // CheckAndRemoveCompletedWqe: a WQE is complete when all its bytes are sent and acknowledged
        if (op->bytesLeft == 0 && op->segments.empty() && !op->done) {
            op->done = true;
            int ti = taskIdx[op->task];
            task_completed(n, ti);
        }
    }
    // TryContinueJettyTransmission(jetty): nothing eligible once the WQE is done
    try_continue_jetty(n, jettyNum);
}

// ----------------------------------------------------------------------------------------------
// TP receive (UbTransportChannel::RecvDataPacket / RecvTpAck)
static void target_done(Tp *tp, int jetty, int task, uint32_t taSsn)
{
    Seg *s = new Seg;
    s->task = task;
    s->jetty = jetty;
    s->resp = true;
    s->size = 1;              // carrier byte of a zero-payload response
    s->bytesLeft = 1;
    s->payloadPerPkt = 0;
    s->psnSize = 1;
    s->taSsn = taSsn;
    s->wireHdr = HDR_TAACK;
    enqueue_remote(tp, s);
}

static void recv_data(Node &n, Tp *tp, int k)
{
    Pkt pk = P[k];
    freepkt(k);
    if (pk.psn < tp->psnRecvNxt)
        die("repeated packet");
    if (pk.psn > tp->psnRecvNxt) {
        // out of order without retransmission: buffered, no response (UpdateReceiveWindow...)
        tp->buffered[pk.psn] = newpkt();
        P[tp->buffered[pk.psn]] = pk;
        return;
    }
    struct Done {
        bool resp;
        int jetty, task;
        uint32_t taSsn;
    };
    std::vector<Done> done;
    auto track = [&](const Pkt &q) {
        auto &u = tp->inbound[{q.srcTpn, q.msn}];
        u.bytes += q.payload;
        if (q.last) {
            done.push_back({q.resp, q.jetty, q.task, q.taSsn});
            tp->inbound.erase({q.srcTpn, q.msn});
        }
    };
    track(pk);
    tp->psnRecvNxt++;
    for (;;) {
        auto it = tp->buffered.find(tp->psnRecvNxt);
        if (it == tp->buffered.end())
            break;
        track(P[it->second]);
        freepkt(it->second);
        tp->buffered.erase(it);
        tp->psnRecvNxt++;
    }
    // ACK
    int a = newpkt();
    Pkt &ap = P[a];
    ap.type = P_ACK;
    ap.size = SZ_TPACK;
    ap.vl = pk.vl;
    ap.sip = pk.dip;
    ap.dip = pk.sip;
    ap.usport = pk.usport;
    ap.udport = pk.udport;
    ap.srcTpn = tp->tpn;
    ap.dstTpn = tp->peerTpn;
    ap.psn = tp->psnRecvNxt - 1;
    ap.last = pk.last;
    tp->ackQ.push_back(a);
    port_trigger(n, tp->port);
    for (auto &d : done) {
        if (d.resp)
            process_ta_response(n, tp, d.jetty, d.task, d.taSsn);
        else
            schedule_now([tp, d]() { target_done(tp, d.jetty, d.task, d.taSsn); });
    }
}

static void recv_ack(Node &n, Tp *tp, int k)
{
    uint64_t acked = P[k].psn;
    freepkt(k);
    uint64_t prev = tp->psnSndUna;
    if (acked >= tp->psnSndUna && acked < tp->psnSndNxt && acked + 1 > tp->psnSndUna)
        tp->psnSndUna = acked + 1;
    if (tp->psnSndUna > prev) {
        if (tp->sendWindowLimited && !tp_inflight_limited(tp)) {
            tp->sendWindowLimited = false;
            port_trigger(n, tp->port);
        }
    }
    // CompleteAckedWqeSegments: requests wait for their TA response; responses retire here
    for (size_t i = 0; i < tp->segs.size();) {
        Seg *s = tp->segs[i];
        if (tp->psnSndUna < s->psnStart + s->psnSize) {
            ++i;
            continue;
        }
        tp->segs.erase(tp->segs.begin() + i);
        if (s->resp)
            delete s;
        if (tp_can_schedule_another(tp))
            try_continue_jetty_tp(tp);
    }
    // UpdateSenderAfterTransportAck
    if (tp_has_work(tp))
        port_trigger(n, tp->port);
    tp_notify(tp);
}

static void receive(Node &n, int p, int k)
{
    Pkt &pk = P[k];
    if (fport)
        fprintf(fport, "%lld %d %d R %u\n", (long long)g_now, n.id, p, pk.size);
    if (pk.type == P_CTRL) {
        Port &pt = n.ports[p];
        for (int v = 0; v < vlNum; ++v)
            if (pk.credits[v] > 0)
                pt.txfree[v] += pk.credits[v] * cbfcGrainCtrl;
        freepkt(k);
        Node *np = &n;
        schedule_now([np, p]() { port_trigger(*np, p); });
        return;
    }
    // OnDataPacketReceived: restore a piggybacked credit
    if (pk.crd) {
        n.ports[p].txfree[pk.crdVl] += cbfcGrainData;
        pk.crd = false;
        pk.crdVl = 0;
        Node *np = &n;
        schedule_now([np, p]() { port_trigger(*np, p); });
    }
    bool local = !n.sw && (pk.dip & 0xffffff00u) == (node_ip(n.id) & 0xffffff00u);
    if (!local) {
        forward(n, p, k);
        return;
    }
    // SinkTpDataPacket: credit returns at once on a host
    accumulate_returned_credit(n, p, k);
    maybe_queue_control_return(n, p);
    auto it = n.tps.find(pk.dstTpn);
    Tp *tp;
    if (it == n.tps.end()) {
        int peer = ip_node(pk.sip);
        tp = local_tp(n, peer, pk.vl);
        if (tp->tpn != pk.dstTpn)
            die("inbound TP mismatch");
    } else
        tp = it->second;
    if (pk.type == P_ACK)
        recv_ack(n, tp, k);
    else
        recv_data(n, tp, k);
}

// ----------------------------------------------------------------------------------------------
static void start_task(int ti)
{
    TaskCfg &t = C.tasks[ti];
    Node &n = N[t.src];
    if (ftask)
        fprintf(ftask, "%lld %d %d S\n", (long long)g_now, n.id, t.id);
    if (t.op != "URMA_WRITE")
        die("unsupported opType " + t.op);
    int jn = n.nextJetty++;
    TS[ti].jetty = jn;
    Tp *tp = local_tp(n, t.dst, t.pri);
    Jetty *j = new Jetty;
    j->num = jn;
    j->tp = tp;
    n.jetties[jn] = j;
    Op *op = new Op;
    op->task = t.id;
    op->jetty = jn;
    op->tp = tp;
    op->size = op->bytesLeft = t.size;
    j->op = op;
    // SubmitWqe -> TriggerScheduleWqeSegment -> StartDispatchGate (no dispatch delay)
    op->dispatchReady = true;
    enqueue_rtp_wqe(op, false);
}

// ----------------------------------------------------------------------------------------------
static void check_attrs()
{
    // attributes the model reads, with the values it supports
    auto &A = C.attr;
    auto need = [&](const std::string &k, const std::string &def, const std::string &ok) {
        std::string v = A.get(k, def);
        if (v != ok)
            die("unsupported " + k + " = " + v + " (model supports " + ok + ")");
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
    vlNum = (int)A.geti("UB_VL_NUM", 16);
    if (vlNum != 16)
        die("UB_VL_NUM must be 16");
    cbfcInit = (int32_t)A.geti("ns3::UbPort::CbfcInitCreditCell", 6553);
    cbfcThld = (int32_t)A.geti("ns3::UbPort::CbfcCtrlCrdRtrThldCell", 1024);
    cbfcGrainData = (int)A.geti("ns3::UbPort::CbfcRetCellGrainDataPacket", 4);
    cbfcGrainCtrl = (int)A.geti("ns3::UbPort::CbfcRetCellGrainControlPacket", 32);
    cbfcCellBytes = (int)(A.geti("ns3::UbPort::CbfcFlitLenByte", 20) * A.geti("ns3::UbPort::CbfcFlitsPerCell", 8));
    reservePerQueue = (uint64_t)A.geti("ns3::UbQueueManager::ReservePerQueueBytes", 1048576);
    inflightMax = (uint64_t)A.geti("ns3::UbTransportChannel::DefaultMaxInflightPacketSize", 1000);
    wqeSegMax = (uint64_t)A.geti("ns3::UbTransportChannel::DefaultMaxWqeSegNum", 1000);
    need("ns3::UbSwitchAllocator::EnableDirectAllocationCompletion", "true", "true");
    need("ns3::UbTransaction::InitiatorProtocolDispatchDelay", "+0ns", "+0ns");
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: twins-ubseq CASE_DIR [--out=PREFIX] [--dep-delay=TIME]\n");
        return 1;
    }
    std::string dir = argv[1], out;
    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        if (a.rfind("--out=", 0) == 0)
            out = a.substr(6);
        else if (a.rfind("--dep-delay=", 0) == 0)
            depDelay = parse_time_ps(a.substr(12));
        else if (a == "--evlog")
            g_evlog = true;
        else
            die("unknown option " + a);
    }
    try {
        load_case(dir, C);
    } catch (std::exception &e) {
        die(e.what());
    }
    check_attrs();
    if (!out.empty()) {
        fport = fopen((out + ".port").c_str(), "w");
        ftask = fopen((out + ".task").c_str(), "w");
    }
    // nodes and links
    N.resize(C.nodes.size());
    for (size_t i = 0; i < N.size(); ++i) {
        Node &n = N[i];
        n.id = (int)i;
        n.sw = C.nodes[i].sw;
        n.alloc = C.nodes[i].alloc_ps;
        n.fwd = C.nodes[i].fwd_ps;
        n.ports.resize(C.nodes[i].ports);
        n.ingressBytes.assign(n.ports.size(), {});
        n.nTpsByPortVl.assign(n.ports.size() * VLN, 0);
        for (auto &pt : n.ports)
            for (int v = 0; v < VLN; ++v)
                pt.txfree[v] = cbfcInit;
    }
    for (auto &l : C.links) {
        Port &a = N[l.n1].ports[l.p1], &b = N[l.n2].ports[l.p2];
        a.peerNode = l.n2; a.peerPort = l.p2; a.bps = l.bps; a.delay = l.delay_ps;
        b.peerNode = l.n1; b.peerPort = l.p1; b.bps = l.bps; b.delay = l.delay_ps;
    }
    // connections reserved in traffic.csv order (ReserveTpnsForTraffic -> ReserveNewTpConnections)
    for (auto &t : C.tasks) {
        if (t.op != "URMA_WRITE" && t.op != "URMA_READ")
            continue;
        if (conns.count({t.src, t.dst, t.pri}))
            continue;
        Node &s = N[t.src], &d = N[t.dst];
        if (d.ports.size() != 1 || s.ports.size() != 1)
            die("multi-port hosts not supported yet");
        Conn *c = new Conn{t.src, 0, s.nextTpn++, t.dst, 0, d.nextTpn++, t.pri};
        conns[{t.src, t.dst, t.pri}] = c;
        conns[{t.dst, t.src, t.pri}] = c;
    }
    for (auto &t : C.tasks)
        if (t.op == "URMA_WRITE" || t.op == "URMA_READ")
            conns.at({t.src, t.dst, t.pri})->uses++;
    // traffic gen
    TS.assign(C.tasks.size(), {});
    for (size_t i = 0; i < C.tasks.size(); ++i) {
        auto &t = C.tasks[i];
        taskIdx[t.id] = (int)i;
        phases[t.phase].total++;
    }
    for (size_t i = 0; i < C.tasks.size(); ++i) {
        auto &t = C.tasks[i];
        if (t.deps.empty()) {
            TS[i].state = 1;
            readyTasks.push_back((int)i);
        } else {
            TS[i].pendingPhases = (int)t.deps.size();
            for (int d : t.deps)
                phases[d].dependents.push_back((int)i);
        }
    }
    // CheckExampleProcess (every 100 us from load) stops the run once all tasks are complete
    std::function<void()> check;
    bool stopped = false;
    check = [&]() {
        if (completedTasks < (int)C.tasks.size()) {
            schedule(-1, 100000000, check);
            return;
        }
        schedule(-1, 0, [&]() { stopped = true; });
    };
    schedule(-1, 100000000, check);
    {
        auto b = collect_ready();
        schedule_tasks(b);
    }
    while (!g_q.empty() && !stopped) {
        Ev *e = g_q.top();
        g_q.pop();
        g_now = e->ts;
        g_ctx = e->node;
        ++g_events;
        e->fn();
        delete e;
    }
    if (fport) fclose(fport);
    if (ftask) fclose(ftask);
    fprintf(stderr, "twins-ubseq: %d/%zu tasks, %llu events, end %lld ps, drops %llu\n", completedTasks,
            C.tasks.size(), (unsigned long long)g_events, (long long)g_now, (unsigned long long)drops);
    return completedTasks == (int)C.tasks.size() ? 0 : 3;
}
