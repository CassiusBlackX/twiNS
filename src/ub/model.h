// TwiNS-UB model: ns-3-ub's UnifiedBus data path as plain-data state and event handlers.
//
// Every handler follows one ns-3-ub function with the same side effects in the same order, and
// every ns-3 Schedule call becomes one call into the engine E, in the same order:
//   e.sched(n, delay, ev)            Simulator::Schedule / ScheduleNow on node n's context
//   e.link(n, port, pkt, delay)      UbLink::TransmitStart: the peer's Receive after delay
//   e.tg(n, delay, ev)               an event of the traffic generator (a global object)
//   e.at_node(node, delay, ev)       from the traffic generator: ScheduleWithContext(node, ...)
// so an engine can order simultaneous events exactly as ns-3 does: by a global uid (seq.cc) or by
// the events' causal lineage (the chain engine). The state uses no STL and fixed-size pools, so the
// same code compiles for the GPU. docs/ub/model.md maps it to ns-3-ub.
#pragma once
#include "ubhash.h"
#include <cmath>
#include <cstdint>
#include <cstring>

#ifndef UB_HD
#if defined(__CUDACC__) || defined(__HIPCC__)
#define UB_HD __host__ __device__
#else
#define UB_HD
#endif
#endif

namespace ub {

// ns-3-ub constants (ub-datatype.h, ub-cbfc.h) and header sizes of the packet kinds modelled
constexpr uint32_t MTU = 4096;
constexpr uint32_t SEG_BYTES = 64 * 1024;
constexpr uint32_t HDR_WRITE = 78;   // DL + network + IPv4 + UDP + TP + BTAH + MAETAH
constexpr uint32_t HDR_TAACK = 62;   // a TA response (TAACK), no payload
constexpr uint32_t SZ_TPACK = 62;    // a TP ACK
constexpr uint32_t SZ_CTRL = 40;     // a CBFC control frame
constexpr int VLN = 16;
constexpr uint32_t EGRESS_MAX = 512 * 1024;
constexpr int MAXV = 4;              // VLs in use (VL 0 for control frames, then data VLs)

enum PktType : uint8_t { P_DATA, P_ACK, P_CTRL };

struct Pkt {
    uint32_t size;
    uint8_t type, vl, crd, crdVl;
    uint8_t last, resp, pad0, pad1;
    uint32_t sip, dip;
    uint16_t usport, udport;
    int32_t srcTpn, dstTpn;
    uint32_t psn, msn;
    int32_t task, jetty;
    uint32_t taSsn, payload;
    uint8_t credits[VLN];
    int32_t next;   // pool link
};

struct List {
    int32_t head, tail, n;
};

enum EvKind : uint8_t {
    E_TXDONE,    // a: port                 UbPort::TransmitComplete
    E_TRIG,      // a: port                 ScheduleNow(UbPort::TriggerTransmit)
    E_ALLOC,     // a: port                 UbSwitchAllocator::RunAllocation
    E_INPROC,    // a: in, b: out, c: pkt   UbSwitch::CompleteInPortProcessing
    E_PREPARE,   // a: op                   UbTransaction::RunTrackedPrepareRtpWqeSegment
    E_ADMIT,     // a: op, b: seg           UbTransaction::RunTrackedAdmitRtpWqeSegment
    E_RUNRTPTX,  // a: tp                   UbTransaction::RunRtpTx
    E_TARGET,    // a: tp, b: jetty, c: task, d: TA SSN   target completion (ScheduleNow)
    E_START,     // a: task                 UbTrafficGen::StartTask
    E_CLEANUP,   // a: task, b: destroy     UbTrafficGen::ApplyTaskCleanupOnOwner
    E_APPLY,     // a: task (traffic gen)   UbTrafficGen::ApplyTaskCompletion
    E_LCREQ,     // a: conn                 the destroy request's part on this node (DestroyConnection)
    E_LCDESTROY2,// a: conn                 the destroy notice to ep2 after ep1 finalized
    E_RECV,      // engine-internal: an arrival
    E_CHECK,     // engine-internal: CheckExampleProcess
    E_STOP,
};

struct Ev {
    uint8_t kind;
    int32_t a, b, c, d;
};

// An allocator ingress queue id: >= 0 a VOQ (its index in World::voq), <= -2 a TP (-2 - tp)
constexpr int32_t IQ_NONE = -1;
UB_HD inline int32_t iq_tp(int32_t tp) { return -2 - tp; }
UB_HD inline bool iq_is_tp(int32_t q) { return q <= -2; }
UB_HD inline int32_t iq_tpi(int32_t q) { return -2 - q; }

struct Port {
    int32_t peerNode, peerPort;
    int64_t delay;
    int32_t txtab;        // offset of this link's tx-time table in World::txt
    uint8_t busy, running, runAgain, allocPending;
    int32_t active;       // the queue selected for the pending allocation
    List egress;
    uint32_t egressBytes;
    uint32_t selectedSlot;
    uint8_t lastPiggyVl;
    uint8_t pad[3];
    uint32_t rrSeeded;    // bit per VL
    int32_t txfree[VLN], toReturn[VLN];
    uint32_t rrIdx[VLN];
    int32_t tpcnt[MAXV];  // registered TPs by VL index
    uint64_t vmask[MAXV]; // bit in: VOQ (this port, VL index, in) is not empty
    int32_t tplist;       // offset in World::tplist: [vlIdx][capacity] TP ids, registration order
};

struct Seg {
    int32_t task, jetty;
    uint8_t resp, payloadPerPkt, inTp, inOp;
    uint32_t size, bytesLeft;
    uint32_t psnStart, psnSize, msn, taSsn, wireHdr;
    int32_t next;     // TP segment list / remote list / free list
    int32_t nextOp;   // the operation's admitted segments, until their TA response
};

struct TxEntry {
    uint8_t remote, pad[3];
    int32_t ref;          // local: op; remote: remote record
    int32_t next;
};

struct Remote {           // TA responses waiting on a TP, by (origin jetty, task)
    int32_t jetty, task;
    List segs;
    uint8_t queued, used, pad[2];
};

struct Tp {
    int32_t node, tpn, peer, peerTpn, port, dport, pri, vi;
    uint32_t sip, dip;
    // sender
    uint32_t psnSndNxt, psnSndUna, tpPsnCnt, msnCnt;
    List segs, ackQ;
    uint8_t sendWindowLimited;
    // RtpTxQueue
    uint8_t txEventPending, txEntryActive, waiting;
    List pending;
    uint8_t activeRemote, pad0[3];
    int32_t activeRef;
    int32_t remote0, nremote;   // Remote records (World::remote)
    // receiver
    uint32_t psnRecvNxt;
    List buffered;
    uint8_t inOpen, pad2[3];    // a TA unit is partly received (m_inboundTaUnits not empty)
    // lifecycle
    int32_t conn;
    uint8_t slot, draining, drainHold, frozen, dead, live, pad1[2];
    int32_t workLeases;
};

struct Op {
    int32_t task, jetty, tp;
    uint32_t size, bytesLeft;
    uint8_t canSend, dispatchReady, txEntryQueued, segTxActive, done, pad[3];
    List segments;              // admitted request segments awaiting their TA response
};

struct Jetty {
    uint8_t bound, used, pad[2];
    int32_t op, tp;
    uint32_t taSsnSndNxt, taSsnSndUna;
    uint64_t acked;             // bit i: TA SSN taSsnSndUna + i acknowledged
};

struct Conn {
    int32_t n1, p1, tpn1, n2, p2, tpn2, pri;
    int32_t uses;
    int32_t ep[3];
    uint8_t destroyRequested, pad[3];
};

struct Node {
    int32_t id;
    uint8_t sw, nv, pad[2];
    int32_t P;
    int64_t alloc, fwd;
    int32_t port0;              // World::port
    int32_t voq0;               // World::voq: [out][vlIdx][in]
    int32_t ingress0;           // World::ingress: [in][vlIdx] bytes
    int32_t vlIdx[VLN];         // VL -> index (-1 unused)
    int32_t vlOf[MAXV];
    int32_t tp0, ntp;           // TPs (World::tp)
    int32_t jetty0, njetty, nextJetty;
    int32_t op0;
    // pools (free lists)
    int32_t pktFree, segFree, entFree, remFree;
    uint32_t tpnBase;           // TP numbers are tp index - tp0 is not the tpn; see tpByTpn
    int32_t tpByTpn0, ntpn;     // World::tpByTpn: local TP number -> TP (or -1)
};

struct TaskG {                  // the traffic generator's view of a task
    int32_t id, src, dst, pri, phase, conn;
    uint32_t size;
    int64_t delay;
    int32_t state, pendingPhases, jetty;
    int32_t dep0, ndep;          // World::depOf (dependent tasks of this task's phase...)
};

struct PhaseG {
    int32_t total, completed;
    int32_t dep0, ndep;          // dependent task indices in World::phaseDeps
    uint8_t released, pad[3];
};

struct Params {
    int32_t vlNum;
    int32_t cbfcInit, cbfcThld, cbfcGrainData, cbfcGrainCtrl, cbfcCellBytes;
    uint32_t reservePerQueue, inflightMax, wqeSegMax;
    int64_t depDelay;
};

// the few scalars the model writes (the World itself is read-only: pointers and parameters)
struct WorldMut {
    int32_t completed, nready, err, pad;
    uint64_t drops;
};

// All state, in flat arrays (host vectors' data or device copies)
struct World {
    WorldMut *mut;
    Params P;
    int32_t nnode, ntask, nphase;
    Node *node;
    Port *port;
    List *voq;
    uint64_t *ingress;
    int32_t *tplist;
    int64_t *txt;
    int32_t txtN;               // entries per table
    Tp *tp;
    int32_t *tpByTpn;
    Op *op;
    Jetty *jetty;
    Conn *conn;
    Pkt *pkt;
    Seg *seg;
    TxEntry *ent;
    Remote *remote;
    TaskG *task;
    PhaseG *phase;
    int32_t *phaseDeps;
    int32_t *taskByJettyOp;     // unused
    int32_t *ready;             // traffic gen ready list (task indices)
};

// ----------------------------------------------------------------------------------------------
// small helpers
#define UB_FAIL(w, code)                                                                           \
    do {                                                                                           \
        if (!(w).mut->err)                                                                         \
            (w).mut->err = (code);                                                                 \
    } while (0)
enum : int32_t {
    ERR_POOL_PKT = 1, ERR_POOL_SEG, ERR_POOL_ENT, ERR_POOL_REM, ERR_DROP, ERR_ROUTE, ERR_NOTP,
    ERR_COMMIT_NULL, ERR_TWO_ALLOC, ERR_REPEAT, ERR_SSN, ERR_REVIVE, ERR_TPLIST, ERR_VL,
};

UB_HD inline void l_init(List &l) { l.head = l.tail = -1, l.n = 0; }

#define UB_LIST_PUSH(l, pool, i)                                                                   \
    do {                                                                                           \
        (pool)[i].next = -1;                                                                       \
        if ((l).tail >= 0)                                                                         \
            (pool)[(l).tail].next = (i);                                                           \
        else                                                                                       \
            (l).head = (i);                                                                        \
        (l).tail = (i);                                                                            \
        (l).n++;                                                                                   \
    } while (0)
#define UB_LIST_PUSH_FRONT(l, pool, i)                                                             \
    do {                                                                                           \
        (pool)[i].next = (l).head;                                                                 \
        (l).head = (i);                                                                            \
        if ((l).tail < 0)                                                                          \
            (l).tail = (i);                                                                        \
        (l).n++;                                                                                   \
    } while (0)

template <class T> UB_HD inline int32_t l_pop(List &l, T *pool)
{
    int32_t i = l.head;
    l.head = pool[i].next;
    if (l.head < 0)
        l.tail = -1;
    l.n--;
    return i;
}
// remove element i, whose predecessor is prev (-1: the head)
template <class T> UB_HD inline void l_unlink(List &l, T *pool, int32_t prev, int32_t i)
{
    int32_t nx = pool[i].next;
    if (prev < 0)
        l.head = nx;
    else
        pool[prev].next = nx;
    if (l.tail == i)
        l.tail = prev;
    l.n--;
}

UB_HD inline int32_t pkt_alloc(World &w, Node &n)
{
    int32_t i = n.pktFree;
    if (i < 0) {
        UB_FAIL(w, ERR_POOL_PKT);
        return 0;
    }
    n.pktFree = w.pkt[i].next;
    memset(&w.pkt[i], 0, sizeof(Pkt));
    return i;
}
UB_HD inline void pkt_free(World &w, Node &n, int32_t i)
{
    w.pkt[i].next = n.pktFree;
    n.pktFree = i;
}
UB_HD inline int32_t seg_alloc(World &w, Node &n)
{
    int32_t i = n.segFree;
    if (i < 0) {
        UB_FAIL(w, ERR_POOL_SEG);
        return 0;
    }
    n.segFree = w.seg[i].next;
    memset(&w.seg[i], 0, sizeof(Seg));
    return i;
}
UB_HD inline void seg_free(World &w, Node &n, int32_t i)
{
    w.seg[i].next = n.segFree;
    n.segFree = i;
}
UB_HD inline int32_t ent_alloc(World &w, Node &n)
{
    int32_t i = n.entFree;
    if (i < 0) {
        UB_FAIL(w, ERR_POOL_ENT);
        return 0;
    }
    n.entFree = w.ent[i].next;
    memset(&w.ent[i], 0, sizeof(TxEntry));
    return i;
}
UB_HD inline void ent_free(World &w, Node &n, int32_t i)
{
    w.ent[i].next = n.entFree;
    n.entFree = i;
}

UB_HD inline uint32_t node_ip(int32_t id) { return 0x0a000000u + (id / 256) * 0x10000u + (id % 256) * 0x100u; }
UB_HD inline uint32_t node_port_ip(int32_t id, int32_t port)
{
    return node_ip(id) + (uint32_t)((port + 1) % 256);
}
UB_HD inline int32_t ip_node(uint32_t ip)
{
    ip -= 0x0a000000u;
    return (int32_t)(((ip & 0x00ff0000u) >> 16) * 256 + ((ip & 0xff00u) >> 8));
}

UB_HD inline int32_t cells(const World &w, uint32_t size)
{
    return (int32_t)ceilf((float)size / (float)w.P.cbfcCellBytes);
}
UB_HD inline int64_t txtime(const World &w, const Port &pt, uint32_t bytes)
{
    return w.txt[pt.txtab + (bytes < (uint32_t)w.txtN ? bytes : (uint32_t)w.txtN - 1)];
}
UB_HD inline Port &port_of(World &w, Node &n, int32_t p) { return w.port[n.port0 + p]; }
UB_HD inline List &voq_of(World &w, Node &n, int32_t out, int32_t vi, int32_t in)
{
    return w.voq[n.voq0 + (out * n.nv + vi) * n.P + in];
}
UB_HD inline uint64_t &ingress_of(World &w, Node &n, int32_t in, int32_t vi) { return w.ingress[n.ingress0 + in * n.nv + vi]; }
UB_HD inline void voq_push(World &w, Node &n, int32_t out, int32_t vi, int32_t in, int32_t k)
{
    List &q = voq_of(w, n, out, vi, in);
    UB_LIST_PUSH(q, w.pkt, k);
    port_of(w, n, out).vmask[vi] |= 1ull << in;
}
// a VOQ by its index: (out, vi, in) from the offset
UB_HD inline int32_t voq_pop(World &w, Node &n, int32_t q)
{
    int32_t k = l_pop(w.voq[q], w.pkt);
    if (!w.voq[q].n) {
        int32_t rel = q - n.voq0, in = rel % n.P, vi = rel / n.P % n.nv, out = rel / n.P / n.nv;
        port_of(w, n, out).vmask[vi] &= ~(1ull << in);
    }
    return k;
}

// ----------------------------------------------------------------------------------------------
template <class E> struct Model {
    E &e;
    World &w;
    UB_HD Model(E &e_, World &w_) : e(e_), w(w_) {}

    // ------------------------------------------------------------------------------------------
    // TP as an allocator ingress queue
    UB_HD bool tp_has_work(Tp &t)
    {
        if (t.ackQ.n)
            return true;
        if (!t.segs.n)
            return false;
        return t.psnSndNxt < t.tpPsnCnt;
    }
    UB_HD bool tp_inflight_limited(Tp &t) { return t.psnSndNxt - t.psnSndUna >= w.P.inflightMax; }
    UB_HD int tp_active_segments(Tp &t)
    {
        int c = 0;
        for (int32_t s = t.segs.head; s >= 0; s = w.seg[s].next)
            c += w.seg[s].bytesLeft != 0;
        return c;
    }
    UB_HD bool tp_can_schedule_another(Tp &t) { return tp_active_segments(t) < 2; }
    UB_HD bool tp_seg_limited(Tp &t) { return (uint32_t)t.segs.n >= w.P.wqeSegMax; }

    UB_HD bool iq_empty(int32_t q)
    {
        if (iq_is_tp(q))
            return !tp_has_work(w.tp[iq_tpi(q)]);
        return w.voq[q].n == 0;
    }
    UB_HD bool iq_limited(int32_t q)
    {
        if (!iq_is_tp(q))
            return false;
        Tp &t = w.tp[iq_tpi(q)];
        if (t.ackQ.n)
            return false;
        if (tp_inflight_limited(t)) {
            t.sendWindowLimited = 1;
            return true;
        }
        return false;
    }
    UB_HD uint32_t seg_next_size(const Seg &s)
    {
        uint32_t progress = s.bytesLeft < MTU ? s.bytesLeft : MTU;
        return (s.payloadPerPkt ? progress : 0) + s.wireHdr;
    }
    UB_HD uint32_t iq_next_size(int32_t q)
    {
        if (!iq_is_tp(q))
            return w.pkt[w.voq[q].head].size;
        Tp &t = w.tp[iq_tpi(q)];
        if (t.ackQ.n)
            return w.pkt[t.ackQ.head].size;
        for (int32_t s = t.segs.head; s >= 0; s = w.seg[s].next)
            if (w.seg[s].bytesLeft)
                return seg_next_size(w.seg[s]);
        return 0;
    }
    UB_HD int32_t iq_vl(int32_t q)
    {
        if (iq_is_tp(q))
            return w.tp[iq_tpi(q)].pri;
        return w.pkt[w.voq[q].head].vl;
    }
    UB_HD bool iq_control(Node &n, int32_t q)
    {
        if (iq_is_tp(q))
            return false;
        int32_t rel = q - n.voq0;   // [out][vi][in]: control VOQ is (p, vl 0, in == p)
        int32_t in = rel % n.P, out = rel / n.P / n.nv;
        return in == out;
    }

    // UbTransportChannel::GetNextPacket (new data path) incl. AdvanceNewDataSendState
    UB_HD int32_t tp_next_packet(Node &n, int32_t ti)
    {
        Tp &t = w.tp[ti];
        if (t.ackQ.n) {
            int32_t k = l_pop(t.ackQ, w.pkt);
            tp_notify(ti);   // PopQueuedPacket -> NotifyLifecycleStateChanged
            return k;
        }
        if (!t.segs.n)
            return -1;
        if (tp_inflight_limited(t)) {
            t.sendWindowLimited = 1;
            return -1;
        }
        int32_t si = -1;
        for (int32_t s = t.segs.head; s >= 0; s = w.seg[s].next)
            if (w.seg[s].bytesLeft) {
                si = s;
                break;
            }
        if (si < 0)
            return -1;
        Seg &s = w.seg[si];
        uint32_t progress = s.bytesLeft < MTU ? s.bytesLeft : MTU;
        uint32_t payload = s.payloadPerPkt ? progress : 0;
        int32_t k = pkt_alloc(w, n);
        Pkt &p = w.pkt[k];
        p.type = P_DATA;
        p.size = payload + s.wireHdr;
        p.vl = (uint8_t)t.pri;
        p.sip = t.sip;
        p.dip = t.dip;
        p.usport = 0;
        p.udport = (uint16_t)t.dport;
        p.srcTpn = t.tpn;
        p.dstTpn = t.peerTpn;
        p.psn = t.psnSndNxt;
        p.msn = s.msn;
        p.last = s.bytesLeft == progress;
        p.resp = s.resp;
        p.task = s.task;
        p.jetty = s.jetty;
        p.taSsn = s.taSsn;
        p.payload = payload;
        s.bytesLeft -= progress;
        t.psnSndNxt++;
        if (s.bytesLeft == 0 && tp_can_schedule_another(t))
            try_continue_jetty_tp(ti);
        return k;
    }

    // ------------------------------------------------------------------------------------------
    // CBFC
    UB_HD bool fc_limited(Node &n, Port &out, int32_t q)
    {
        if (iq_control(n, q))
            return false;
        uint32_t sz = iq_next_size(q);
        int32_t vl = iq_vl(q);
        return out.txfree[vl] < cells(w, sz);
    }
    UB_HD void maybe_queue_control_return(Node &n, int32_t p)
    {
        Port &pt = port_of(w, n, p);
        bool force = false;
        for (int v = 0; v < VLN; ++v)
            if (pt.toReturn[v] >= w.P.cbfcThld)
                force = true;
        if (!force)
            return;
        for (;;) {
            uint8_t cr[VLN];
            bool any = false;
            for (int v = 0; v < VLN; ++v) {
                cr[v] = 0;
                if (v >= w.P.vlNum)
                    continue;
                int32_t left = pt.toReturn[v];
                if (left >= w.P.cbfcGrainCtrl) {
                    int32_t g = left / w.P.cbfcGrainCtrl;
                    if (g > 63)
                        g = 63;
                    if (g > 0) {
                        cr[v] = (uint8_t)g;
                        if (pt.toReturn[v] >= g * w.P.cbfcGrainCtrl)
                            pt.toReturn[v] -= g * w.P.cbfcGrainCtrl;
                        any = true;
                    }
                }
            }
            if (!any)
                break;
            int32_t k = pkt_alloc(w, n);
            w.pkt[k].type = P_CTRL;
            w.pkt[k].size = SZ_CTRL;
            w.pkt[k].vl = 0;
            for (int v = 0; v < VLN; ++v)
                w.pkt[k].credits[v] = cr[v];
            // UbSwitch::SendControlFrame: VOQ[p][0][p], then TriggerTransmit
            voq_push(w, n, p, n.vlIdx[0], p, k);
            port_trigger(n, p);
        }
    }
    UB_HD void accumulate_returned_credit(Node &n, int32_t p, const Pkt &pk)
    {
        port_of(w, n, p).toReturn[pk.vl] += cells(w, pk.size);
    }
    UB_HD void fc_on_egress_enqueued(Node &n, Port &pt, int32_t q, int32_t k)
    {
        if (iq_control(n, q))
            return;
        Pkt &pk = w.pkt[k];
        int32_t c = cells(w, pk.size);
        if (pt.txfree[pk.vl] >= c)
            pt.txfree[pk.vl] -= c;
        if (pk.crd)
            return;
        for (int off = 1; off <= w.P.vlNum; ++off) {
            uint8_t cand = (uint8_t)((pt.lastPiggyVl + off) % w.P.vlNum);
            if (pt.toReturn[cand] >= w.P.cbfcGrainData) {
                pk.crd = 1;
                pk.crdVl = cand;
                pt.toReturn[cand] -= w.P.cbfcGrainData;
                pt.lastPiggyVl = cand;
                return;
            }
        }
    }

    // ------------------------------------------------------------------------------------------
    // allocator (UbRoundRobinAllocator)
    UB_HD int32_t select_next(Node &n, int32_t p)
    {
        Port &pt = port_of(w, n, p);
        int32_t P0 = n.P;
        for (int vi = 0; vi < n.nv; ++vi) {
            int32_t pi = n.vlOf[vi];
            uint32_t slots = (uint32_t)(P0 + pt.tpcnt[vi]);
            if (!(pt.rrSeeded >> pi & 1)) {
                pt.rrIdx[pi] = (uint32_t)((n.id + p) % slots);
                pt.rrSeeded |= 1u << pi;
            } else if (pt.rrIdx[pi] >= slots)
                pt.rrIdx[pi] %= slots;
            uint32_t start = pt.rrIdx[pi];
            int32_t best = IQ_NONE, wrap = IQ_NONE;
            uint32_t bs = 0, ws = 0;
            // VOQs (slot = in port), then TPs (slot = P + ordinal)
            for (uint64_t m = pt.vmask[vi]; m; m &= m - 1) {
                int32_t in = __builtin_ctzll(m);
                int32_t q = (int32_t)(&voq_of(w, n, p, vi, in) - w.voq);
                if (fc_limited(n, pt, q))
                    continue;
                uint32_t slot = (uint32_t)in;
                if (slot >= start) {
                    if (best == IQ_NONE || slot < bs) best = q, bs = slot;
                } else if (wrap == IQ_NONE || slot < ws)
                    wrap = q, ws = slot;
            }
            const int32_t *tl = w.tplist + pt.tplist + vi * n.ntp;
            for (int32_t o = 0; o < pt.tpcnt[vi]; ++o) {
                int32_t q = iq_tp(tl[o]);
                uint32_t slot = (uint32_t)(P0 + o);
                if (iq_empty(q) || iq_limited(q))
                    continue;
                if (fc_limited(n, pt, q))
                    continue;
                if (slot >= start) {
                    if (best == IQ_NONE || slot < bs) best = q, bs = slot;
                } else if (wrap == IQ_NONE || slot < ws)
                    wrap = q, ws = slot;
            }
            int32_t c = best != IQ_NONE ? best : wrap;
            uint32_t cs = best != IQ_NONE ? bs : ws;
            if (c != IQ_NONE) {
                e.log_select(n, p, vi, start, cs, slots);
                pt.selectedSlot = cs;
                pt.rrIdx[pi] = (cs + 1) % slots;
                return c;
            }
            e.log_select(n, p, vi, start, ~0u, slots);
        }
        return IQ_NONE;
    }
    UB_HD bool has_nonempty_ingress(Node &n, int32_t p)
    {
        Port &pt = port_of(w, n, p);
        for (int vi = 0; vi < n.nv; ++vi) {
            if (pt.vmask[vi])
                return true;
            const int32_t *tl = w.tplist + pt.tplist + vi * n.ntp;
            for (int32_t o = 0; o < pt.tpcnt[vi]; ++o)
                if (tp_has_work(w.tp[tl[o]]))
                    return true;
        }
        return false;
    }
    UB_HD void allocate_next(Node &n, int32_t p)
    {
        Port &pt = port_of(w, n, p);
        pt.allocPending = 0;
        bool owned = pt.running;
        int32_t q = pt.active;
        if (q != IQ_NONE) {
            uint32_t nsz = iq_next_size(q);
            if (pt.egressBytes + nsz > EGRESS_MAX) {
                pt.rrIdx[iq_vl(q)] = pt.selectedSlot;
                pt.active = IQ_NONE;
                pt.running = 0;
                return;
            }
            int32_t k;
            if (iq_is_tp(q)) {
                k = tp_next_packet(n, iq_tpi(q));
                if (k < 0) {
                    UB_FAIL(w, ERR_COMMIT_NULL);
                    return;
                }
            } else
                k = voq_pop(w, n, q);
            UB_LIST_PUSH(pt.egress, w.pkt, k);
            pt.egressBytes += w.pkt[k].size;
            fc_on_egress_enqueued(n, pt, q, k);
            if (!iq_is_tp(q) && !iq_control(n, q)) {
                int32_t in = (q - n.voq0) % n.P;
                Pkt &pk = w.pkt[k];
                ingress_of(w, n, in, n.vlIdx[pk.vl]) -= pk.size;
                if (in != p) {
                    accumulate_returned_credit(n, in, pk);
                    maybe_queue_control_return(n, in);
                }
            }
        }
        pt.active = IQ_NONE;
        if (pt.egress.n && !pt.busy)
            dequeue_packet(n, p);   // NotifyAllocationFinish
        bool again = owned && pt.runAgain && has_nonempty_ingress(n, p);
        pt.runAgain = 0;
        pt.running = 0;
        if (again)
            trigger_allocator(n, p);
    }
    UB_HD void trigger_allocator(Node &n, int32_t p)
    {
        Port &pt = port_of(w, n, p);
        if (pt.running) {
            pt.runAgain = 1;
            return;
        }
        pt.running = 1;
        pt.runAgain = 0;
        pt.active = select_next(n, p);
        if (pt.allocPending)
            UB_FAIL(w, ERR_TWO_ALLOC);
        pt.allocPending = 1;
        Ev ev{E_ALLOC, p, 0, 0, 0};
        e.sched(n, n.alloc, ev);
    }

    // ------------------------------------------------------------------------------------------
    // port
    UB_HD void transmit_packet(Node &n, int32_t p, int32_t k)
    {
        Port &pt = port_of(w, n, p);
        Pkt &pk = w.pkt[k];
        e.trace_tx(n, p, pk);
        int64_t tx = txtime(w, pt, pk.size);
        e.link(n, p, pk, tx + pt.delay);
        pkt_free(w, n, k);
        Ev ev{E_TXDONE, p, 0, 0, 0};
        e.sched(n, tx, ev);
    }
    UB_HD void dequeue_packet(Node &n, int32_t p)
    {
        Port &pt = port_of(w, n, p);
        pt.busy = 1;
        int32_t k = l_pop(pt.egress, w.pkt);
        pt.egressBytes -= w.pkt[k].size;
        if (!pt.egress.n)
            trigger_allocator(n, p);
        transmit_packet(n, p, k);
    }
    UB_HD void port_trigger(Node &n, int32_t p)
    {
        Port &pt = port_of(w, n, p);
        if (pt.busy) {
            if (!pt.egress.n)
                trigger_allocator(n, p);
            return;
        }
        if (!pt.egress.n) {
            trigger_allocator(n, p);
            return;
        }
        dequeue_packet(n, p);
    }
    UB_HD void tx_done(Node &n, int32_t p)
    {
        port_of(w, n, p).busy = 0;
        Ev ev{E_TRIG, p, 0, 0, 0};
        e.sched(n, 0, ev);
    }

    // ------------------------------------------------------------------------------------------
    // routing: per-flow shortest paths, HASH64 (UbRoutingProcess)
    UB_HD int32_t route(Node &n, const Pkt &pk, int32_t inPort)
    {
        int32_t cand[64];
        int32_t nc = e.route_candidates(n, pk.dip, inPort, cand);
        if (nc <= 0) {
            UB_FAIL(w, ERR_ROUTE);
            return 0;
        }
        uint8_t b[17];
        uint32_t salt = node_ip(n.id);
        b[0] = pk.sip >> 24; b[1] = pk.sip >> 16; b[2] = pk.sip >> 8; b[3] = pk.sip;
        b[4] = pk.dip >> 24; b[5] = pk.dip >> 16; b[6] = pk.dip >> 8; b[7] = pk.dip;
        b[8] = pk.usport >> 8; b[9] = pk.usport; b[10] = pk.udport >> 8; b[11] = pk.udport;
        b[12] = pk.vl;
        b[13] = salt >> 24; b[14] = salt >> 16; b[15] = salt >> 8; b[16] = salt;
        uint64_t h = ns3_hash64(b, 17);
        return cand[h % (uint64_t)nc];
    }
    UB_HD void voq_enqueue(Node &n, int32_t in, int32_t out, int32_t k)
    {
        voq_push(w, n, out, n.vlIdx[w.pkt[k].vl], in, k);
        port_trigger(n, out);
    }
    UB_HD void forward(Node &n, int32_t in, int32_t k)
    {
        Pkt &pk = w.pkt[k];
        int32_t out = route(n, pk, in);
        int32_t vi = n.vlIdx[pk.vl];
        if (pk.vl == 0 || vi < 0) {
            UB_FAIL(w, ERR_VL);
            return;
        }
        uint64_t &ib = ingress_of(w, n, in, vi);
        if (ib + pk.size > w.P.reservePerQueue) {
            w.mut->drops++;
            UB_FAIL(w, ERR_DROP);
            return;
        }
        ib += pk.size;
        if (n.fwd <= 0)
            voq_enqueue(n, in, out, k);
        else {
            Ev ev{E_INPROC, in, out, k, 0};
            e.sched(n, n.fwd, ev);
        }
    }

    // ------------------------------------------------------------------------------------------
    // connections and TPs
    UB_HD int32_t create_tp(Node &n, int32_t ci, bool first)
    {
        Conn &c = w.conn[ci];
        int32_t tpn = first ? c.tpn1 : c.tpn2;
        int32_t ti = w.tpByTpn[n.tpByTpn0 + tpn];
        Tp &t = w.tp[ti];
        if (t.live || t.dead) {
            UB_FAIL(w, ERR_REVIVE);
            return ti;
        }
        t.live = 1;
        // registration with the allocator (UbSwitch::RegisterTpWithAllocator)
        Port &pt = port_of(w, n, t.port);
        int32_t *tl = w.tplist + pt.tplist + t.vi * n.ntp;
        if (pt.tpcnt[t.vi] >= n.ntp) {
            UB_FAIL(w, ERR_TPLIST);
            return ti;
        }
        tl[pt.tpcnt[t.vi]++] = ti;
        c.ep[first ? 1 : 2] = ti;
        return ti;
    }
    // the local TP endpoint of a connection, materialized on first use (ReconstructTp /
    // CreateReservedTpEndpoint)
    UB_HD int32_t local_tp(Node &n, int32_t ci)
    {
        Conn &c = w.conn[ci];
        bool first = c.n1 == n.id;
        int32_t ti = w.tpByTpn[n.tpByTpn0 + (first ? c.tpn1 : c.tpn2)];
        if (w.tp[ti].live)
            return ti;
        if (c.destroyRequested) {
            UB_FAIL(w, ERR_REVIVE);
            return ti;
        }
        return create_tp(n, ci, first);
    }

    // connection teardown, applied synchronously like single-threaded ns-3-ub
    UB_HD bool tp_quiescent(int32_t ti)
    {
        Tp &t = w.tp[ti];
        if (t.ackQ.n || t.segs.n || t.psnSndUna != t.psnSndNxt)
            return false;
        if (t.buffered.n || t.workLeases)
            return false;
        if (t.pending.n || t.txEntryActive || t.inOpen)
            return false;
        for (int32_t r = 0; r < t.nremote; ++r)
            if (w.remote[t.remote0 + r].used)
                return false;
        Node &n = w.node[t.node];
        for (int32_t j = 0; j < n.nextJetty; ++j) {
            Jetty &jt = w.jetty[n.jetty0 + j];
            if (jt.bound && jt.tp == ti)
                return false;
        }
        return true;
    }
    UB_HD void finalize_tp(int32_t ti)
    {
        Tp &t = w.tp[ti];
        Node &n = w.node[t.node];
        Port &pt = port_of(w, n, t.port);
        int32_t *tl = w.tplist + pt.tplist + t.vi * n.ntp;
        int32_t m = 0;
        for (int32_t o = 0; o < pt.tpcnt[t.vi]; ++o)
            if (tl[o] != ti)
                tl[m++] = tl[o];
        pt.tpcnt[t.vi] = m;
        t.dead = 1;
        t.live = 0;
        e.trace_tp_destroy(n, t);
        endpoint_destroyed(t.conn, t.slot);
    }
    UB_HD void tp_notify(int32_t ti)
    {
        Tp &t = w.tp[ti];
        if (t.dead || !t.draining || t.drainHold || !tp_quiescent(ti))
            return;
        finalize_tp(ti);
    }
    UB_HD void destroy_endpoint(int32_t ci, int slot)
    {
        int32_t ti = w.conn[ci].ep[slot];
        if (ti < 0 || w.tp[ti].dead) {
            endpoint_destroyed(ci, slot);   // NOT_FOUND: never materialized
            return;
        }
        Tp &t = w.tp[ti];
        t.drainHold = 0;
        t.draining = 1;
        t.frozen = 1;
        tp_notify(ti);
    }
    // ep1 finalized: the owner asks the peer to destroy ep2 (OnEndpointDestroyed ->
    // RequestDestroyNotice), synchronously in ns-3: on another node, through the engine
    UB_HD void endpoint_destroyed(int32_t ci, int slot)
    {
        if (slot == 1)
            e.lc_destroy2(w.conn[ci].n2, ci);
    }
    // the parts of DestroyConnectionOwned on one node: the freeze notice to ep2 (n2), the destroy
    // notice to ep1 (n1); ns-3 applies the freeze first
    UB_HD void lc_freeze2(int32_t ci)
    {
        Conn &c = w.conn[ci];
        if (c.ep[2] >= 0 && !w.tp[c.ep[2]].dead) {
            w.tp[c.ep[2]].drainHold = 1;
            w.tp[c.ep[2]].frozen = 1;
        }
    }
    UB_HD void lc_destroy1(int32_t ci) { destroy_endpoint(ci, 1); }
    UB_HD void lc_destroy2(int32_t ci) { destroy_endpoint(ci, 2); }
    // the destroy request at node n: its own parts, in ns-3's order
    UB_HD void lc_request(Node &n, int32_t ci)
    {
        Conn &c = w.conn[ci];
        if (c.n2 == n.id)
            lc_freeze2(ci);
        if (c.n1 == n.id)
            lc_destroy1(ci);
    }
    UB_HD void destroy_connection(Node &n, int32_t ci)
    {
        Conn &c = w.conn[ci];
        if (c.destroyRequested)
            return;
        c.destroyRequested = 1;
        // the other endpoint's node acts at the same instant: synchronously (seq engine) or as an
        // event the traffic generator sent with the cleanup (parallel engines)
        int32_t other = c.n1 == n.id ? c.n2 : c.n1;
        if (c.n2 == n.id)
            lc_freeze2(ci);
        else
            e.lc_peer(other, ci, 0);   // freeze ep2 on n2, before ep1's destroy
        if (c.n1 == n.id)
            lc_destroy1(ci);
        else
            e.lc_peer(other, ci, 1);   // destroy ep1 on n1
    }

    // ------------------------------------------------------------------------------------------
    // transaction layer: RTP Tx queues
    UB_HD void finish_active(Op &op)
    {
        Tp &t = w.tp[op.tp];
        if (t.txEntryActive && !t.activeRemote && t.activeRef == (int32_t)(&op - w.op)) {
            t.txEntryActive = 0;
            t.activeRef = -1;
        }
    }
    UB_HD void schedule_rtp_tx(Node &n, int32_t ti)
    {
        Tp &t = w.tp[ti];
        if (t.waiting || !t.pending.n || t.txEventPending || t.txEntryActive)
            return;
        t.txEventPending = 1;
        Ev ev{E_RUNRTPTX, ti, 0, 0, 0};
        e.sched(n, 0, ev);
    }
    UB_HD void try_continue_rtp_queue(Node &n, int32_t ti)
    {
        Tp &t = w.tp[ti];
        if (!t.waiting || !t.pending.n || t.txEventPending)
            return;
        t.waiting = 0;
        schedule_rtp_tx(n, ti);
    }
    UB_HD void prepare_segment(Node &n, int32_t oi)
    {
        Op &op = w.op[oi];
        Jetty &j = w.jetty[n.jetty0 + op.jetty];
        if (op.bytesLeft) {
            int32_t si = seg_alloc(w, n);
            Seg &s = w.seg[si];
            s.task = op.task;
            s.jetty = op.jetty;
            s.size = op.bytesLeft < SEG_BYTES ? op.bytesLeft : SEG_BYTES;
            s.bytesLeft = s.size;
            s.payloadPerPkt = 1;
            s.psnSize = (s.size + MTU - 1) / MTU;
            s.taSsn = j.taSsnSndNxt;
            s.wireHdr = HDR_WRITE;
            Ev ev{E_ADMIT, oi, si, 0, 0};
            e.sched(n, 0, ev);
        } else {
            op.segTxActive = 0;
            finish_active(op);
            schedule_rtp_tx(n, op.tp);
        }
    }
    UB_HD void start_segment_tx(Node &n, int32_t oi)
    {
        Op &op = w.op[oi];
        if (op.done || !op.dispatchReady || op.segTxActive)
            return;
        op.segTxActive = 1;
        Ev ev{E_PREPARE, oi, 0, 0, 0};
        e.sched(n, 0, ev);
    }
    UB_HD void run_rtp_tx(Node &n, int32_t ti)
    {
        Tp &t = w.tp[ti];
        t.txEventPending = 0;
        if (t.txEntryActive || t.waiting)
            return;
        while (t.pending.n) {
            int32_t ei = l_pop(t.pending, w.ent);
            TxEntry en = w.ent[ei];
            ent_free(w, n, ei);
            if (en.remote) {
                run_remote(n, ti, en.ref);
                break;
            }
            Op &op = w.op[en.ref];
            if (op.done)
                continue;
            op.txEntryQueued = 0;
            t.txEntryActive = 1;
            t.activeRemote = 0;
            t.activeRef = en.ref;
            start_segment_tx(n, en.ref);
            break;
        }
    }
    UB_HD void enqueue_rtp_wqe(Node &n, int32_t oi, bool front)
    {
        Op &op = w.op[oi];
        if (op.done || op.segTxActive)
            return;
        int32_t ti = op.tp;
        Tp &t = w.tp[ti];
        if (!op.txEntryQueued) {
            int32_t ei = ent_alloc(w, n);
            w.ent[ei].remote = 0;
            w.ent[ei].ref = oi;
            if (front)
                UB_LIST_PUSH_FRONT(t.pending, w.ent, ei);
            else
                UB_LIST_PUSH(t.pending, w.ent, ei);
            op.txEntryQueued = 1;
        }
        if (!front && !t.txEntryActive && !t.txEventPending && !t.waiting)
            run_rtp_tx(n, ti);
    }
    UB_HD bool tp_admit(Tp &t, int32_t si)
    {
        if (tp_seg_limited(t) || !tp_can_schedule_another(t))
            return false;
        Seg &s = w.seg[si];
        s.msn = t.msnCnt;
        s.psnStart = t.tpPsnCnt;
        t.tpPsnCnt += s.psnSize;
        t.msnCnt++;
        UB_LIST_PUSH(t.segs, w.seg, si);
        s.inTp = 1;
        return true;
    }
    UB_HD void admit_segment(Node &n, int32_t oi, int32_t si)
    {
        Op &op = w.op[oi];
        int32_t ti = op.tp;
        Tp &t = w.tp[ti];
        Jetty &j = w.jetty[n.jetty0 + op.jetty];
        Seg &s = w.seg[si];
        int result;   // 0 admitted, 1 stale, 2 deferred
        if (s.taSsn != j.taSsnSndNxt || op.bytesLeft < s.size)
            result = 1;
        else if (tp_seg_limited(t) || !tp_can_schedule_another(t))
            result = 2;
        else {
            tp_admit(t, si);
            t.workLeases++;
            // the operation keeps the segment until its TA response (OperationRecord::segments)
            s.inOp = 1;
            s.nextOp = -1;
            if (op.segments.tail >= 0)
                w.seg[op.segments.tail].nextOp = si;
            else
                op.segments.head = si;
            op.segments.tail = si;
            op.segments.n++;
            op.canSend = 1;
            op.bytesLeft -= s.size;
            j.taSsnSndNxt++;
            result = 0;
        }
        op.segTxActive = 0;
        finish_active(op);
        if (result == 0) {
            port_trigger(n, t.port);   // WqeSegmentTriggerPortTransmit
            if (op.bytesLeft)
                enqueue_rtp_wqe(n, oi, false);   // TriggerScheduleWqeSegment
            else
                schedule_rtp_tx(n, ti);
        } else if (result == 1) {
            seg_free(w, n, si);
            enqueue_rtp_wqe(n, oi, false);
        } else {
            seg_free(w, n, si);
            enqueue_rtp_wqe(n, oi, true);
            t.waiting = 1;
        }
    }
    // remote (TA response) segments on a TP
    UB_HD int32_t remote_find(Tp &t, int32_t jetty, int32_t task, bool create)
    {
        int32_t freeSlot = -1;
        for (int32_t r = 0; r < t.nremote; ++r) {
            Remote &R = w.remote[t.remote0 + r];
            if (R.used && R.jetty == jetty && R.task == task)
                return t.remote0 + r;
            if (!R.used && freeSlot < 0)
                freeSlot = t.remote0 + r;
        }
        if (!create)
            return -1;
        if (freeSlot < 0) {
            UB_FAIL(w, ERR_POOL_REM);
            return t.remote0;
        }
        Remote &R = w.remote[freeSlot];
        R.used = 1;
        R.jetty = jetty;
        R.task = task;
        R.queued = 0;
        l_init(R.segs);
        return freeSlot;
    }
    UB_HD void run_remote(Node &n, int32_t ti, int32_t ri)
    {
        Tp &t = w.tp[ti];
        Remote &R = w.remote[ri];
        if (!R.used)
            return;
        R.queued = 0;
        if (!R.segs.n) {
            R.used = 0;
            return;
        }
        int32_t si = R.segs.head;
        // TryContinueAcceptedWqeSegment
        if (tp_seg_limited(t) || !tp_can_schedule_another(t)) {
            int32_t ei = ent_alloc(w, n);
            w.ent[ei].remote = 1;
            w.ent[ei].ref = ri;
            UB_LIST_PUSH_FRONT(t.pending, w.ent, ei);
            R.queued = 1;
            t.waiting = 1;
            return;
        }
        l_pop(R.segs, w.seg);
        tp_admit(t, si);
        if (R.segs.n) {
            int32_t ei = ent_alloc(w, n);
            w.ent[ei].remote = 1;
            w.ent[ei].ref = ri;
            UB_LIST_PUSH(t.pending, w.ent, ei);
            R.queued = 1;
        } else
            R.used = 0;
        port_trigger(n, t.port);
        schedule_rtp_tx(n, ti);
    }
    UB_HD void enqueue_remote(Node &n, int32_t ti, int32_t si)
    {
        Tp &t = w.tp[ti];
        Seg &s = w.seg[si];
        int32_t ri = remote_find(t, s.jetty, s.task, true);
        Remote &R = w.remote[ri];
        UB_LIST_PUSH(R.segs, w.seg, si);
        if (!R.queued) {
            int32_t ei = ent_alloc(w, n);
            w.ent[ei].remote = 1;
            w.ent[ei].ref = ri;
            UB_LIST_PUSH(t.pending, w.ent, ei);
            R.queued = 1;
        }
        if (!t.txEntryActive && !t.txEventPending && !t.waiting)
            run_rtp_tx(n, ti);
    }
    UB_HD void try_continue_jetty(Node &n, int32_t jn)
    {
        Jetty &j = w.jetty[n.jetty0 + jn];
        if (j.op < 0)
            return;
        Op &op = w.op[j.op];
        if (op.done || !op.dispatchReady || op.bytesLeft == 0)
            return;
        enqueue_rtp_wqe(n, j.op, false);
    }
    UB_HD void try_continue_jetty_tp(int32_t ti)
    {
        Tp &t = w.tp[ti];
        if (tp_seg_limited(t) || !tp_can_schedule_another(t))
            return;
        Node &n = w.node[t.node];
        try_continue_rtp_queue(n, ti);
        for (int32_t jn = 0; jn < n.nextJetty; ++jn) {
            Jetty &j = w.jetty[n.jetty0 + jn];
            if (j.bound && j.tp == ti)
                try_continue_jetty(n, jn);
        }
    }

    // ------------------------------------------------------------------------------------------
    // initiator: TA responses
    UB_HD void process_ta_response(Node &n, int32_t jn, int32_t task, uint32_t taSsn)
    {
        Jetty &j = w.jetty[n.jetty0 + jn];
        Op &op = w.op[j.op];
        if (op.task != task) {
            UB_FAIL(w, ERR_SSN);
            return;
        }
        int32_t prev = -1, si = op.segments.head;
        while (si >= 0 && w.seg[si].taSsn != taSsn)
            prev = si, si = w.seg[si].nextOp;
        if (si < 0) {
            UB_FAIL(w, ERR_SSN);
            return;
        }
        {
            int32_t nx = w.seg[si].nextOp;
            if (prev < 0)
                op.segments.head = nx;
            else
                w.seg[prev].nextOp = nx;
            if (op.segments.tail == si)
                op.segments.tail = prev;
            op.segments.n--;
            w.seg[si].inOp = 0;
            if (!w.seg[si].inTp)
                seg_free(w, n, si);
        }
        Tp &t = w.tp[op.tp];
        t.workLeases--;
        tp_notify(op.tp);
        if (taSsn < j.taSsnSndUna || taSsn >= j.taSsnSndNxt || taSsn - j.taSsnSndUna >= 64) {
            UB_FAIL(w, ERR_SSN);
            return;
        }
        j.acked |= 1ull << (taSsn - j.taSsnSndUna);
        uint32_t old = j.taSsnSndUna;
        while (j.taSsnSndUna < j.taSsnSndNxt && (j.acked & 1)) {
            j.acked >>= 1;
            j.taSsnSndUna++;
        }
        if (j.taSsnSndUna > old && op.bytesLeft == 0 && op.segments.n == 0 && !op.done) {
            op.done = 1;
            task_completed(n, op.task);
        }
        try_continue_jetty(n, jn);
    }

    // ------------------------------------------------------------------------------------------
    // TP receive
    UB_HD void target_done(Node &n, int32_t ti, int32_t jetty, int32_t task, uint32_t taSsn)
    {
        int32_t si = seg_alloc(w, n);
        Seg &s = w.seg[si];
        s.task = task;
        s.jetty = jetty;
        s.resp = 1;
        s.size = 1;
        s.bytesLeft = 1;
        s.payloadPerPkt = 0;
        s.psnSize = 1;
        s.taSsn = taSsn;
        s.wireHdr = HDR_TAACK;
        enqueue_remote(n, ti, si);
    }
    UB_HD void recv_data(Node &n, int32_t ti, const Pkt &pk)
    {
        Tp &t = w.tp[ti];
        if (pk.psn < t.psnRecvNxt) {
            UB_FAIL(w, ERR_REPEAT);
            return;
        }
        if (pk.psn > t.psnRecvNxt) {
            // out of order without retransmission: buffered, no response
            int32_t k = pkt_alloc(w, n);
            w.pkt[k] = pk;
            int32_t prev = -1, cur = t.buffered.head;
            while (cur >= 0 && w.pkt[cur].psn < pk.psn)
                prev = cur, cur = w.pkt[cur].next;
            w.pkt[k].next = cur;
            if (prev < 0)
                t.buffered.head = k;
            else
                w.pkt[prev].next = k;
            if (cur < 0)
                t.buffered.tail = k;
            t.buffered.n++;
            return;
        }
        // contiguous: complete the TA units whose last packet arrived
        int32_t doneJ[8], doneT[8];
        uint32_t doneS[8];
        uint8_t doneR[8];
        int nd = 0;
        Pkt cur = pk;
        for (;;) {
            t.inOpen = !cur.last;
            if (cur.last && nd < 8) {
                doneJ[nd] = cur.jetty, doneT[nd] = cur.task, doneS[nd] = cur.taSsn, doneR[nd] = cur.resp;
                ++nd;
            }
            t.psnRecvNxt++;
            if (t.buffered.n && w.pkt[t.buffered.head].psn == t.psnRecvNxt) {
                int32_t k = l_pop(t.buffered, w.pkt);
                cur = w.pkt[k];
                pkt_free(w, n, k);
                continue;
            }
            break;
        }
        int32_t a = pkt_alloc(w, n);
        Pkt &ap = w.pkt[a];
        ap.type = P_ACK;
        ap.size = SZ_TPACK;
        ap.vl = pk.vl;
        ap.sip = pk.dip;
        ap.dip = pk.sip;
        ap.usport = pk.usport;
        ap.udport = pk.udport;
        ap.srcTpn = t.tpn;
        ap.dstTpn = t.peerTpn;
        ap.psn = t.psnRecvNxt - 1;
        ap.last = pk.last;
        UB_LIST_PUSH(t.ackQ, w.pkt, a);
        port_trigger(n, t.port);
        for (int i = 0; i < nd; ++i) {
            if (doneR[i])
                process_ta_response(n, doneJ[i], doneT[i], doneS[i]);
            else {
                Ev ev{E_TARGET, ti, doneJ[i], doneT[i], (int32_t)doneS[i]};
                e.sched(n, 0, ev);
            }
        }
    }
    UB_HD void recv_ack(Node &n, int32_t ti, const Pkt &pk)
    {
        Tp &t = w.tp[ti];
        uint32_t acked = pk.psn;
        uint32_t prev = t.psnSndUna;
        if (acked >= t.psnSndUna && acked < t.psnSndNxt)
            t.psnSndUna = acked + 1;
        if (t.psnSndUna > prev && t.sendWindowLimited && !tp_inflight_limited(t)) {
            t.sendWindowLimited = 0;
            port_trigger(n, t.port);
        }
        // CompleteAckedWqeSegments: requests wait for their TA response, responses retire here
        int32_t pv = -1, si = t.segs.head;
        while (si >= 0) {
            Seg &s = w.seg[si];
            int32_t nx = s.next;
            if (t.psnSndUna < s.psnStart + s.psnSize) {
                pv = si;
                si = nx;
                continue;
            }
            l_unlink(t.segs, w.seg, pv, si);
            s.inTp = 0;
            if (!s.inOp)
                seg_free(w, n, si);
            si = nx;
            if (tp_can_schedule_another(t))
                try_continue_jetty_tp(ti);
        }
        if (tp_has_work(t))
            port_trigger(n, t.port);
        tp_notify(ti);
    }
    UB_HD void receive(Node &n, int32_t p, Pkt pk)
    {
        e.trace_rx(n, p, pk);
        if (pk.type == P_CTRL) {
            Port &pt = port_of(w, n, p);
            for (int v = 0; v < w.P.vlNum; ++v)
                if (pk.credits[v])
                    pt.txfree[v] += pk.credits[v] * w.P.cbfcGrainCtrl;
            Ev ev{E_TRIG, p, 0, 0, 0};
            e.sched(n, 0, ev);
            return;
        }
        if (pk.crd) {
            port_of(w, n, p).txfree[pk.crdVl] += w.P.cbfcGrainData;
            pk.crd = 0;
            pk.crdVl = 0;
            Ev ev{E_TRIG, p, 0, 0, 0};
            e.sched(n, 0, ev);
        }
        bool local = !n.sw && (pk.dip & 0xffffff00u) == (node_ip(n.id) & 0xffffff00u);
        if (!local) {
            int32_t k = pkt_alloc(w, n);
            w.pkt[k] = pk;
            forward(n, p, k);
            return;
        }
        accumulate_returned_credit(n, p, pk);
        maybe_queue_control_return(n, p);
        int32_t ti = pk.dstTpn < n.ntpn ? w.tpByTpn[n.tpByTpn0 + pk.dstTpn] : -1;
        if (ti < 0) {
            UB_FAIL(w, ERR_NOTP);
            return;
        }
        if (!w.tp[ti].live)
            ti = local_tp(n, w.tp[ti].conn);
        if (pk.type == P_ACK)
            recv_ack(n, ti, pk);
        else
            recv_data(n, ti, pk);
    }

    // ------------------------------------------------------------------------------------------
    // tasks
    UB_HD void task_completed(Node &n, int32_t ti)
    {
        TaskG &t = w.task[ti];
        e.trace_task(n, t, 'C');
        if (t.state != 2)
            return;
        Ev ev{E_APPLY, ti, 0, 0, 0};
        e.tg(n, w.P.depDelay, ev);
    }
    UB_HD void start_task(Node &n, int32_t tix)
    {
        TaskG &t = w.task[tix];
        e.trace_task(n, t, 'S');
        int32_t jn = n.nextJetty++;
        t.jetty = jn;
        int32_t ti = local_tp(n, t.conn);
        Jetty &j = w.jetty[n.jetty0 + jn];
        j.bound = 1;
        j.used = 1;
        j.tp = ti;
        int32_t oi = n.op0 + jn;
        j.op = oi;
        Op &op = w.op[oi];
        op.task = tix;
        op.jetty = jn;
        op.tp = ti;
        op.size = op.bytesLeft = t.size;
        l_init(op.segments);
        op.dispatchReady = 1;
        enqueue_rtp_wqe(n, oi, false);
    }
    UB_HD void cleanup(Node &n, int32_t tix, bool destroy)
    {
        TaskG &t = w.task[tix];
        Jetty &j = w.jetty[n.jetty0 + t.jetty];
        j.bound = 0;
        tp_notify(j.tp);
        if (destroy)
            destroy_connection(n, t.conn);
    }

    // ------------------------------------------------------------------------------------------
    UB_HD void dispatch(Node &n, const Ev &ev)
    {
        switch (ev.kind) {
        case E_LCREQ: lc_request(n, ev.a); break;
        case E_LCDESTROY2: lc_destroy2(ev.a); break;
        case E_TXDONE: tx_done(n, ev.a); break;
        case E_TRIG: port_trigger(n, ev.a); break;
        case E_ALLOC: allocate_next(n, ev.a); break;
        case E_INPROC: voq_enqueue(n, ev.a, ev.b, ev.c); break;
        case E_PREPARE: prepare_segment(n, ev.a); break;
        case E_ADMIT: admit_segment(n, ev.a, ev.b); break;
        case E_RUNRTPTX: run_rtp_tx(n, ev.a); break;
        case E_TARGET: target_done(n, ev.a, ev.b, ev.c, (uint32_t)ev.d); break;
        case E_START: start_task(n, ev.a); break;
        case E_CLEANUP: cleanup(n, ev.a, ev.b != 0); break;
        default: break;
        }
    }
};

// ----------------------------------------------------------------------------------------------
// The traffic generator (UbTrafficGen), a global object. Its events are E_APPLY.
template <class E> struct TrafficGen {
    E &e;
    World &w;
    UB_HD TrafficGen(E &e_, World &w_) : e(e_), w(w_) {}

    UB_HD void schedule_ready()
    {
        // CollectReadyTaskBatchLocked: sorted by task id (the ready list is kept sorted)
        for (int32_t i = 0; i < w.mut->nready; ++i) {
            int32_t ti = w.ready[i];
            TaskG &t = w.task[ti];
            if (t.state != 1)
                continue;
            t.state = 2;
            Ev ev{E_START, ti, 0, 0, 0};
            e.at_node(t.src, t.delay, ev);
        }
        w.mut->nready = 0;
    }
    UB_HD void ready_insert(int32_t ti)
    {
        int32_t i = w.mut->nready++;
        while (i > 0 && w.task[w.ready[i - 1]].id > w.task[ti].id) {
            w.ready[i] = w.ready[i - 1];
            --i;
        }
        w.ready[i] = ti;
    }
    UB_HD void apply(int32_t ti)
    {
        TaskG &t = w.task[ti];
        if (t.state == 3)
            return;
        t.state = 3;
        w.mut->completed++;
        PhaseG &ph = w.phase[t.phase];
        ph.completed++;
        if (ph.completed >= ph.total && !ph.released) {
            ph.released = 1;
            for (int32_t i = 0; i < ph.ndep; ++i) {
                int32_t d = w.phaseDeps[ph.dep0 + i];
                if (w.task[d].state != 0)
                    continue;
                if (--w.task[d].pendingPhases == 0) {
                    w.task[d].state = 1;
                    ready_insert(d);
                }
            }
        }
        schedule_ready();
        // the task's cleanup request (task-scoped Jetty, planned TP connections)
        Conn &c = w.conn[t.conn];
        bool destroy = --c.uses == 0;
        Ev ev{E_CLEANUP, ti, destroy ? 1 : 0, 0, 0};
        e.at_node(t.src, 0, ev);
        if (destroy) {
            // the other endpoint's node takes its part at the cleanup's instant and place in the
            // event order (parallel engines; the seq engine applies it inside the cleanup)
            int32_t other = c.n1 == t.src ? c.n2 : c.n1;
            Ev lc{E_LCREQ, t.conn, 0, 0, 0};
            e.at_node_twin(other, lc);
        }
    }
};

} // namespace ub
