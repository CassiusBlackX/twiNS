// twins-ub: model.h on the GPU, the slot-synchronous engine of par.cc with one block per node.
//
// Per slot: tg_slot runs the traffic generator's events below the slot's end (one block), then
// node_slot runs every node's (one block each, lane 0: a node's events are a sequence), then
// late_slot applies the late destroy notices of TP connection peers with the allocator repair (one
// thread), and next_slot finds where the next slot starts. Arrivals and messages go into the
// receiving node's inbox ring (atomic tail); a node moves its inbox into its heap at the start of
// its slot. Events order by (time, chain) like par.cc. See par.cc and docs/ub/parallel.md.
//
// Usage: twins-ub CASE_DIR [--out=PREFIX] [--dep-delay=TIME] [--pool=SCALE] [--notrace]
#define TWINS_USE_HIP 1
#include "gpu_runtime.h"
#include "ub/chain.h"
#include "ub/setup.h"
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <vector>

using namespace ub;

#define CK(x)                                                                                      \
    do {                                                                                           \
        cudaError_t e_ = (x);                                                                      \
        if (e_ != cudaSuccess) {                                                                   \
            fprintf(stderr, "twins-ub: %s at %s:%d\n", cudaGetErrorString(e_), __FILE__, __LINE__); \
            exit(4);                                                                               \
        }                                                                                          \
    } while (0)

struct DEnt {
    int64_t ts;
    uint64_t seq;
    Chain c;
    Ev ev;
    Pkt pk;
};
struct HK {               // a heap entry: the event's time inline, its pool index
    int64_t ts;
    int32_t idx, pad;
};
struct DQ {               // a node's (or the traffic generator's) event queue
    int32_t heapN, freeN;
    int32_t fhead, ftail;  // FIFO of this instant's zero-delay events (pool indices)
    uint32_t inHead, inTail;
    int32_t ebase, ecap, ibase, icap;
    uint64_t seqc;
    int32_t sbase, scap, selN;
    int32_t pad;
};
struct DSel {
    int64_t ts;
    Chain c;
    int32_t port, vi;
    uint32_t start, chosen, slots, pad;
    uint64_t cand[4];
};
struct DLate {
    int32_t node;
    int64_t ts;
    Chain c;
    Ev ev;
};
struct TRec {
    int64_t ts;
    int32_t node, a;      // port, or the task id
    uint32_t size, seq;
    uint8_t kind, pad[7]; // 'T' 'R' 'S' 'C'
};
struct Dev {
    World w;
    ChainCtx cx;
    DQ *q;
    DEnt *ent;            // event pools
    HK *heap;
    int32_t *efree, *fifo;
    DEnt *inbox;
    DSel *sel;
    DLate *late;
    uint32_t *lateN;
    uint32_t lateCap;
    TRec *tr;
    unsigned long long *trN;
    uint64_t trCap;
    uint32_t *trSeq;      // per node
    int32_t *rtOff, *rtCnt, *rtPorts;
    int32_t nq;           // nnode + 1
    int64_t stopAt;
    unsigned long long *counters; // [0] events [1] late [2] repairs [3] conflicts [4] overflow
    int trace;
};

__device__ Dev g_d;

struct GEngine;
typedef Model<GEngine> GM;

__device__ __forceinline__ bool ent_before(const ChainCtx &x, const DEnt &a, const DEnt &b)
{
    if (a.ts != b.ts)
        return a.ts < b.ts;
    bool inexact;
    int c = ccmp(x, a.c, b.c, &inexact);
    if (inexact)
        UB_ATOMIC_ADD(&x.stat[1], 1ull);
    if (c == 0) {
        bool same = true;
        const uint64_t *pa = (const uint64_t *)&a.c, *pb = (const uint64_t *)&b.c;
        for (size_t i = 0; i < sizeof(Chain) / 8; ++i)
            if (pa[i] != pb[i]) {
                same = false;
                break;
            }
        if (!same)
            UB_ATOMIC_ADD(&x.stat[0], 1ull);
        return a.seq < b.seq;
    }
    return c < 0;
}

#ifdef UB_NODE_PROF
__device__ unsigned long long g_sect[8], g_hmax, g_hsum;
#define SECT_BEGIN long long sc0_ = clock64();
#define SECT_END(i) atomicAdd(&g_sect[i], (unsigned long long)(clock64() - sc0_));
#else
#define SECT_BEGIN
#define SECT_END(i)
#endif
__device__ __forceinline__ int32_t pool_get(const Dev &d, DQ &q)
{
    if (q.freeN == 0) {
        UB_ATOMIC_ADD(&d.counters[4], 1ull);
        UB_FAIL(d.w, 100);
        return 0;
    }
    return d.efree[q.ebase + --q.freeN];
}
__device__ __forceinline__ bool hk_before(const Dev &d, const DQ &q, const HK &a, const HK &b)
{
    if (a.ts != b.ts)
        return a.ts < b.ts;
    return ent_before(d.cx, d.ent[q.ebase + a.idx], d.ent[q.ebase + b.idx]);
}
__device__ void heap_insert(const Dev &d, DQ &q, int32_t i)
{
    SECT_BEGIN
    HK *h = d.heap + q.ebase;
    HK x{d.ent[q.ebase + i].ts, i, 0};
    int32_t k = q.heapN++;
#ifdef UB_NODE_PROF
    atomicMax(&g_hmax, (unsigned long long)q.heapN);
    atomicAdd(&g_hsum, (unsigned long long)q.heapN);
#endif
    while (k > 0) {
        int32_t p = (k - 1) / 2;
        if (!hk_before(d, q, x, h[p]))
            break;
        h[k] = h[p];
        k = p;
    }
    h[k] = x;
    SECT_END(0)
}
__device__ void heap_push(const Dev &d, DQ &q, const DEnt &e)
{
    int32_t i = pool_get(d, q);
    DEnt &slot = d.ent[q.ebase + i];
    slot = e;
    slot.seq = q.seqc++;
    heap_insert(d, q, i);
}
__device__ int32_t heap_pop(const Dev &d, DQ &q)
{
    SECT_BEGIN
    HK *h = d.heap + q.ebase;
    int32_t top = h[0].idx;
    HK x = h[--q.heapN];
    int32_t k = 0, n = q.heapN;
    for (;;) {
        int32_t l = 2 * k + 1;
        if (l >= n)
            break;
        int32_t m = l;
        if (l + 1 < n && hk_before(d, q, h[l + 1], h[l]))
            m = l + 1;
        if (!hk_before(d, q, h[m], x))
            break;
        h[k] = h[m];
        k = m;
    }
    if (n)
        h[k] = x;
    SECT_END(1)
    return top;
}
// a slot in a node's inbox, to fill in place
__device__ DEnt *inbox_slot(const Dev &d, int32_t node)
{
    DQ &q = d.q[node];
    uint32_t pos = atomicAdd(&q.inTail, 1u);
    if (pos - q.inHead >= (uint32_t)q.icap) {
        UB_ATOMIC_ADD(&d.counters[4], 1ull);
        UB_FAIL(d.w, 101);
        return &d.inbox[q.ibase];   // (the run stops)
    }
    return &d.inbox[q.ibase + pos % q.icap];
}

struct GEngine {
    const Dev &d;
    World &w;
    int32_t node;
    int64_t now_;
    const Chain *cur;
    uint64_t ord;
    const Chain *lastChild;
    int64_t lastChildTs;
    GM *M;
    __device__ GEngine(const Dev &dd, int32_t n) : d(dd), w(const_cast<World &>(dd.w)), node(n), now_(0), cur(nullptr), ord(0), lastChild(nullptr), lastChildTs(0), M(nullptr) {}
    __device__ int64_t now() const { return now_; }
    __device__ void child(Chain &out, int64_t delay) { cpush(d.cx, &out, ccode(delay, ord++), *cur); }
    __device__ void sched(Node &n, int64_t delay, Ev ev)
    {
        DQ &q = d.q[n.id];
        int32_t i = pool_get(d, q);
        DEnt &e = d.ent[q.ebase + i];
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = ev;
        e.seq = q.seqc++;
        if (delay == 0) {
            // this instant's zero-delay events run after all of its scheduled-earlier ones, in
            // the order they were scheduled (their uids)
            d.fifo[q.ebase + (q.ftail++ % q.ecap)] = i;
        } else
            heap_insert(d, q, i);
    }
    __device__ void link(Node &n, int32_t p, const Pkt &pk, int64_t delay)
    {
        Port &pt = port_of(w, n, p);
        DEnt &e = *inbox_slot(d, pt.peerNode);
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = Ev{E_RECV, pt.peerPort, 0, 0, 0};
        e.pk = pk;
    }
    __device__ void tg(Node &, int64_t delay, Ev ev)
    {
        DEnt &e = *inbox_slot(d, w.nnode);
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = ev;
    }
    __device__ void at_node(int32_t nd, int64_t delay, Ev ev)
    {
        DEnt &e = *inbox_slot(d, nd);
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = ev;
        lastChild = &e.c;
        lastChildTs = e.ts;
    }
    __device__ void at_node_twin(int32_t nd, Ev ev)
    {
        DEnt &e = *inbox_slot(d, nd);
        e.ts = lastChildTs;
        e.c = *lastChild;
        e.ev = ev;
    }
    __device__ void lc_peer(int32_t, int32_t, int) {}
    __device__ void lc_destroy2(int32_t nd, int32_t ci)
    {
        uint32_t i = atomicAdd(d.lateN, 1u);
        UB_ATOMIC_ADD(&d.counters[1], 1ull);
        if (i >= d.lateCap) {
            UB_FAIL(w, 102);
            return;
        }
        d.late[i].node = nd;
        d.late[i].ts = now_;
        d.late[i].c = *cur;
        d.late[i].ev = Ev{E_LCDESTROY2, ci, 0, 0, 0};
    }
    __device__ bool port_has_held_tp(Node &n, int32_t p, int32_t vi)
    {
        Port &pt = port_of(w, n, p);
        const int32_t *tl = w.tplist + pt.tplist + vi * n.ntp;
        for (int32_t o = 0; o < pt.tpcnt[vi]; ++o)
            if (w.tp[tl[o]].drainHold)
                return true;
        return false;
    }
    __device__ void log_select(Node &n, int32_t p, int32_t vi, uint32_t start, uint32_t chosen, uint32_t slots)
    {
        if (!port_has_held_tp(n, p, vi))
            return;
        DQ &q = d.q[n.id];
        if (q.selN >= q.scap) {
            UB_ATOMIC_ADD(&d.counters[3], 1ull);   // cannot repair: counted as a conflict
            return;
        }
        DSel &L = d.sel[q.sbase + q.selN++];
        L.ts = now_;
        L.c = *cur;
        L.port = p;
        L.vi = vi;
        L.start = start;
        L.chosen = chosen;
        L.slots = slots;
        for (int k = 0; k < 4; ++k) L.cand[k] = 0;
        Port &pt = port_of(w, n, p);
        for (int32_t in = 0; in < n.P; ++in) {
            int32_t qq = (int32_t)(&voq_of(w, n, p, vi, in) - w.voq);
            if (w.voq[qq].n && !M->fc_limited(n, pt, qq) && in < 256)
                L.cand[in >> 6] |= 1ull << (in & 63);
        }
        const int32_t *tl = w.tplist + pt.tplist + vi * n.ntp;
        for (int32_t o = 0; o < pt.tpcnt[vi]; ++o) {
            int32_t qq = iq_tp(tl[o]);
            uint32_t slot = n.P + o;
            if (!M->iq_empty(qq) && !M->iq_limited(qq) && !M->fc_limited(n, pt, qq) && slot < 256)
                L.cand[slot >> 6] |= 1ull << (slot & 63);
        }
    }
    __device__ int32_t route_candidates(Node &n, uint32_t dip, int32_t inPort, int32_t *cand)
    {
        return ub_route(w, n.id, dip, inPort, cand);
    }
    __device__ void trace(uint8_t kind, int32_t nd, int32_t a, uint32_t size)
    {
        if (!d.trace)
            return;
        unsigned long long i = UB_ATOMIC_ADD(d.trN, 1ull);
        if (i >= d.trCap)
            return;
        TRec &r = d.tr[i];
        r.ts = now_;
        r.node = nd;
        r.a = a;
        r.size = size;
        r.seq = d.trSeq[nd]++;
        r.kind = kind;
    }
    __device__ void trace_tx(Node &n, int32_t p, const Pkt &pk) { trace('T', n.id, p, pk.size); }
    __device__ void trace_rx(Node &n, int32_t p, const Pkt &pk) { trace('R', n.id, p, pk.size); }
    __device__ void trace_task(Node &n, const TaskG &t, char c) { trace((uint8_t)c, n.id, t.id, 0); }
    __device__ void trace_tp_destroy(Node &, const Tp &) {}
    __device__ void trace_conn_destroy(int32_t, int32_t) {}
};

// ----------------------------------------------------------------------------------------------
#ifdef UB_NODE_PROF
__device__ unsigned long long g_evn2[4096];
#endif
__device__ void run_queue(int32_t id, int64_t end)
{
    Dev d = g_d;          // read-only: a private copy keeps the base pointers in registers
    World &w = d.w;
    DQ &q = d.q[id];
    // inbox -> heap
    uint32_t tail = q.inTail;
    for (uint32_t i = q.inHead; i != tail; ++i)
        heap_push(d, q, d.inbox[q.ibase + i % q.icap]);
    q.inHead = tail;
    GEngine E(d, id);
    GM M(E, w);
    E.M = &M;
    TrafficGen<GEngine> G(E, w);
    uint64_t n = 0;
    (void)G;
    for (;;) {
        int32_t i;
        bool fromFifo = false;
        if (q.fhead != q.ftail) {
            // the FIFO holds events of the current instant; a heap event of the same instant runs
            // first if it was scheduled earlier (a lead) or its chain says so (zero-delay events
            // of other entities, such as task starts and cleanups)
            int32_t fi = d.fifo[q.ebase + (q.fhead % q.ecap)];
            const DEnt &fe = d.ent[q.ebase + fi];
            if (d.stopAt >= 0 && fe.ts == d.stopAt)
                break;
            bool heapFirst = false;
            if (q.heapN > 0 && d.heap[q.ebase].ts == fe.ts) {
                const DEnt &he = d.ent[q.ebase + d.heap[q.ebase].idx];
                heapFirst = ((he.c.r[0].code >> ORD_BITS) - 1) > 0 || ent_before(d.cx, he, fe);
            }
            if (heapFirst)
                i = heap_pop(d, q);
            else {
                i = fi;
                q.fhead++;
                fromFifo = true;
            }
        } else {
            if (q.heapN == 0)
                break;
            const DEnt &top = d.ent[q.ebase + d.heap[q.ebase].idx];
            if (top.ts >= end)
                break;
            if (d.stopAt >= 0 && top.ts == d.stopAt && ((top.c.r[0].code >> ORD_BITS) - 1) == 0)
                break;
            i = heap_pop(d, q);
        }
        (void)fromFifo;
        DEnt &e = d.ent[q.ebase + i];
        E.now_ = e.ts;
        E.cur = &e.c;
        E.ord = 0;
        ++n;
        SECT_BEGIN
        if (id == w.nnode)
            G.apply(e.ev.a);
        else if (e.ev.kind == E_RECV)
            M.receive(w.node[id], e.ev.a, e.pk);
        else
            M.dispatch(w.node[id], e.ev);
        SECT_END(2)
        d.efree[q.ebase + q.freeN++] = i;   // the event's slot is reused only after it ran
        if (w.mut->err)
            break;
    }
    if (n)
        UB_ATOMIC_ADD(&d.counters[0], n);
#ifdef UB_NODE_PROF
    if (id < 4096) g_evn2[id] += n;
#endif
}

__global__ void tg_slot(int64_t end)
{
    if (threadIdx.x == 0)
        run_queue(g_d.w.nnode, end);
}
__device__ unsigned long long g_cyc[4096], g_evn[4096];
__global__ void node_slot(int64_t end)
{
    if (threadIdx.x == 0 && (int32_t)blockIdx.x < g_d.w.nnode) {
#ifdef UB_NODE_PROF
        long long c0 = clock64();
        unsigned long long e0 = g_d.counters[0];
#endif
        run_queue(blockIdx.x, end);
#ifdef UB_NODE_PROF
        if (blockIdx.x < 4096) {
            g_cyc[blockIdx.x] += clock64() - c0;
        }
#endif
    }
}

// the late destroy notices, in event order, with the allocator repair (see par.cc apply_late)
__global__ void late_slot()
{
    if (threadIdx.x || blockIdx.x)
        return;
    Dev d = g_d;
    World &w = d.w;
    uint32_t nl = *d.lateN;
    if (nl > d.lateCap)
        nl = d.lateCap;
    // insertion sort (a few per slot)
    for (uint32_t i = 1; i < nl; ++i) {
        DLate x = d.late[i];
        uint32_t j = i;
        while (j > 0) {
            DEnt a, b;
            a.ts = x.ts, a.c = x.c, a.seq = 0;
            b.ts = d.late[j - 1].ts, b.c = d.late[j - 1].c, b.seq = 1;
            if (!ent_before(d.cx, a, b))
                break;
            d.late[j] = d.late[j - 1];
            --j;
        }
        d.late[j] = x;
    }
    for (uint32_t li = 0; li < nl; ++li) {
        DLate &m = d.late[li];
        Node &n = w.node[m.node];
        GEngine E(d, m.node);
        GM M(E, w);
        E.M = &M;
        E.now_ = m.ts;
        E.cur = &m.c;
        E.ord = 0;
        int32_t ci = m.ev.a;
        int32_t ti = w.conn[ci].ep[2];
        if (ti < 0 || w.tp[ti].dead) {
            M.lc_destroy2(ci);
            continue;
        }
        Tp &t = w.tp[ti];
        Port &pt = port_of(w, n, t.port);
        const int32_t *tl = w.tplist + pt.tplist + t.vi * n.ntp;
        int32_t ordX = -1;
        for (int32_t o = 0; o < pt.tpcnt[t.vi]; ++o)
            if (tl[o] == ti)
                ordX = o;
        uint32_t sX = n.P + ordX;
        DQ &q = d.q[m.node];
        DEnt probe;
        probe.ts = m.ts;
        probe.c = m.c;
        probe.seq = ~0ull;
        int32_t first = -1, cnt = 0;
        for (int32_t k = 0; k < q.selN; ++k) {
            DSel &L = d.sel[q.sbase + k];
            if (L.port != t.port || L.vi != t.vi)
                continue;
            DEnt le;
            le.ts = L.ts;
            le.c = L.c;
            le.seq = 0;
            if (ent_before(d.cx, probe, le)) {
                if (first < 0)
                    first = k;
                ++cnt;
            }
        }
        bool quiescent = M.tp_quiescent(ti);
        if (!quiescent || cnt == 0) {
            M.lc_destroy2(ci);
            continue;
        }
        UB_ATOMIC_ADD(&d.counters[2], 1ull);
        uint32_t r = d.sel[q.sbase + first].start;
        bool ok = true;
        for (int32_t k = first; k < q.selN && ok; ++k) {
            DSel &L = d.sel[q.sbase + k];
            if (L.port != t.port || L.vi != t.vi)
                continue;
            DEnt le;
            le.ts = L.ts;
            le.c = L.c;
            le.seq = 0;
            if (!ent_before(d.cx, probe, le))
                continue;
            uint32_t slotsP = L.slots - 1;
            int32_t best = -1, wrap = -1;
            for (uint32_t s = 0; s < L.slots && s < 256; ++s) {
                if (!(L.cand[s >> 6] >> (s & 63) & 1))
                    continue;
                if (s == sX) {
                    ok = false;
                    break;
                }
                uint32_t sp = s > sX ? s - 1 : s;
                if (sp >= r) {
                    if (best < 0 || sp < (uint32_t)best) best = (int32_t)sp;
                } else if (wrap < 0 || sp < (uint32_t)wrap)
                    wrap = (int32_t)sp;
            }
            if (!ok)
                break;
            int32_t chosenP = best >= 0 ? best : wrap;
            int32_t ours = L.chosen == ~0u ? -1 : (int32_t)(L.chosen > sX ? L.chosen - 1 : L.chosen);
            if (chosenP != ours)
                ok = false;
            else if (chosenP >= 0)
                r = (uint32_t)(chosenP + 1) % slotsP;
        }
        if (!ok)
            UB_ATOMIC_ADD(&d.counters[3], 1ull);
        M.lc_destroy2(ci);
        if (ok)
            pt.rrIdx[n.vlOf[t.vi]] = r;
    }
    *d.lateN = 0;
    for (int32_t i = 0; i < d.nq; ++i)
        d.q[i].selN = 0;
}

// the earliest pending event (heap tops and inboxes)
__global__ void next_slot(unsigned long long *out)
{
    const Dev &d = g_d;
    int32_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= d.nq)
        return;
    DQ &q = d.q[i];
    int64_t m = INT64_MAX;
    if (q.heapN)
        m = d.heap[q.ebase].ts;
    if (q.fhead != q.ftail) {
        int64_t t = d.ent[q.ebase + d.fifo[q.ebase + (q.fhead % q.ecap)]].ts;
        if (t < m)
            m = t;
    }
    for (uint32_t k = q.inHead; k != q.inTail; ++k) {
        int64_t t = d.inbox[q.ibase + k % q.icap].ts;
        if (t < m)
            m = t;
    }
    atomicMin(out, (unsigned long long)m);
}

// ----------------------------------------------------------------------------------------------
template <class T> static T *dalloc(size_t n)
{
    T *p;
    CK(cudaMalloc(&p, sizeof(T) * std::max<size_t>(n, 1)));
    return p;
}
template <class T> static T *dcopy(const std::vector<T> &v)
{
    T *p = dalloc<T>(v.size());
    if (!v.empty())
        CK(cudaMemcpy(p, v.data(), sizeof(T) * v.size(), cudaMemcpyHostToDevice));
    return p;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: twins-ub CASE_DIR [--out=PREFIX] [--dep-delay=TIME] [--pool=SCALE] [--notrace]\n");
        return 1;
    }
    auto t0 = std::chrono::steady_clock::now();
    std::string dir = argv[1], out;
    int64_t depDelay = 100000;
    double pool = 1.0;
    bool trace = true;
    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        if (a.rfind("--out=", 0) == 0)
            out = a.substr(6);
        else if (a.rfind("--dep-delay=", 0) == 0)
            depDelay = parse_time_ps(a.substr(12));
        else if (a.rfind("--pool=", 0) == 0)
            pool = atof(a.c_str() + 7);
        else if (a == "--notrace")
            trace = false;
        else {
            fprintf(stderr, "twins-ub: unknown option %s\n", a.c_str());
            return 1;
        }
    }
    if (out.empty())
        trace = false;
    Case C;
    HostWorld H;
    try {
        load_case(dir, C);
    } catch (std::exception &e) {
        fprintf(stderr, "twins-ub: %s\n", e.what());
        return 2;
    }
    std::string err = build_world(C, H, depDelay, pool);
    if (!err.empty()) {
        fprintf(stderr, "twins-ub: %s\n", err.c_str());
        return 2;
    }
    World &hw = H.w;
    int64_t S = depDelay;
    for (auto &l : C.links)
        S = std::min(S, l.delay_ps);
    if (S <= 0) {
        fprintf(stderr, "twins-ub: a zero link delay leaves no lookahead\n");
        return 2;
    }
    const int32_t NN = hw.nnode, NQ = NN + 1;
    // queues
    std::vector<DQ> q(NQ);
    int32_t eoff = 0, ioff = 0, soff = 0;
    for (int32_t i = 0; i < NQ; ++i) {
        int32_t P = i < NN ? H.node[i].P : 1, nj = i < NN ? H.node[i].njetty : hw.ntask;
        q[i].ecap = (int32_t)((256 + P * 320 + 2 * nj) * pool);
        q[i].icap = (int32_t)((128 + P * 96 + 2 * nj) * pool);
        if (i == NN) {
            q[i].ecap = (int32_t)((256 + 2 * hw.ntask) * pool);
            q[i].icap = q[i].ecap;
        }
        q[i].scap = i < NN ? 64 * P : 0;
        q[i].ebase = eoff, eoff += q[i].ecap;
        q[i].ibase = ioff, ioff += q[i].icap;
        q[i].sbase = soff, soff += q[i].scap;
        q[i].freeN = q[i].ecap;
    }
    std::vector<int32_t> efree(eoff);
    for (int32_t i = 0; i < NQ; ++i)
        for (int32_t k = 0; k < q[i].ecap; ++k)
            efree[q[i].ebase + k] = q[i].ecap - 1 - k;
    // initial task starts (roots) into inboxes, built on the host
    std::vector<DEnt> inbox(ioff);
    {
        int32_t nroot = 0;
        for (int32_t k = 0; k < H.mut.nready; ++k) {
            int32_t ti = H.ready[k];
            TaskG &t = H.task[ti];
            if (t.state != 1)
                continue;
            t.state = 2;
            DEnt e{};
            e.ts = t.delay;
            Chain r;
            chain_root(&r, (uint32_t)++nroot);
            // a root chain: one run, no tail (host side)
            memset(&e.c, 0, sizeof(Chain));
            e.c.r[0].code = ccode(t.delay, 0);
            e.c.r[0].cnt = 1;
            e.c.root = r.root;
            e.c.hash = chash(0, e.c.r[0].code);
            e.ev = Ev{E_START, ti, 0, 0, 0};
            DQ &qq = q[t.src];
            if (qq.inTail - qq.inHead >= (uint32_t)qq.icap) {
                fprintf(stderr, "twins-ub: inbox too small for the tasks at load (use --pool)\n");
                return 2;
            }
            inbox[qq.ibase + qq.inTail++ % qq.icap] = e;
        }
        H.mut.nready = 0;
    }
    // device copies
    Dev d{};
    d.w = hw;
    d.w.node = dcopy(H.node);
    d.w.port = dcopy(H.port);
    d.w.voq = dcopy(H.voq);
    d.w.ingress = dcopy(H.ingress);
    d.w.tplist = dcopy(H.tplist);
    d.w.txt = dcopy(H.txt);
    d.w.tp = dcopy(H.tp);
    d.w.tpByTpn = dcopy(H.tpByTpn);
    d.w.op = dcopy(H.op);
    d.w.jetty = dcopy(H.jetty);
    d.w.conn = dcopy(H.conn);
    d.w.pkt = dcopy(H.pkt);
    d.w.seg = dcopy(H.seg);
    d.w.ent = dcopy(H.ent);
    d.w.remote = dcopy(H.remote);
    d.w.task = dcopy(H.task);
    d.w.phase = dcopy(H.phase);
    d.w.phaseDeps = dcopy(H.phaseDeps);
    d.w.ready = dcopy(H.ready);
    d.w.mut = dcopy(std::vector<WorldMut>{H.mut});
    d.w.hostPort0 = dcopy(H.hostPort0);
    d.w.rtList = dcopy(H.rtList);
    d.w.rlOff = dcopy(H.rlOff);
    d.w.rlCnt = dcopy(H.rlCnt);
    d.w.rlPorts = dcopy(H.rlPorts);
    // chains
    const uint64_t TAILN = 1ull << 24, MEMON = 1ull << 22, WALKN = 1ull << 20;
    d.cx.tail = dalloc<TailNode>(TAILN);
    CK(cudaMemset(d.cx.tail, 0, sizeof(TailNode) * TAILN));
    d.cx.mask = TAILN - 1;
    d.cx.cnt = dalloc<uint64_t>(1);
    CK(cudaMemset(d.cx.cnt, 0, 8));
    std::vector<uint32_t> rr(hw.ntask + 2);
    for (size_t i = 0; i < rr.size(); ++i) rr[i] = (uint32_t)i;
    d.cx.rootRank = dcopy(rr);
    d.cx.memo = dalloc<unsigned long long>(MEMON);
    CK(cudaMemset(d.cx.memo, 0, 8 * MEMON));
    d.cx.memoMask = MEMON - 1;
    d.cx.walk = dalloc<unsigned long long>(WALKN);
    CK(cudaMemset(d.cx.walk, 0, 8 * WALKN));
    d.cx.walkMask = WALKN - 1;
    d.cx.stat = dalloc<unsigned long long>(8);
    CK(cudaMemset(d.cx.stat, 0, 64));
    // queues
    d.q = dcopy(q);
    d.ent = dalloc<DEnt>(eoff);
    d.heap = dalloc<HK>(eoff);
    d.fifo = dalloc<int32_t>(eoff);
    d.efree = dcopy(efree);
    d.inbox = dcopy(inbox);
    d.sel = dalloc<DSel>(soff);
    d.lateCap = 1 << 16;
    d.late = dalloc<DLate>(d.lateCap);
    d.lateN = dalloc<uint32_t>(1);
    CK(cudaMemset(d.lateN, 0, 4));
    d.trace = trace;
    d.trCap = trace ? (uint64_t)64 << 20 : 1;
    d.tr = dalloc<TRec>(d.trCap);
    d.trN = dalloc<unsigned long long>(1);
    CK(cudaMemset(d.trN, 0, 8));
    d.trSeq = dalloc<uint32_t>(NQ);
    CK(cudaMemset(d.trSeq, 0, 4 * NQ));
    d.nq = NQ;
    d.stopAt = -1;
    d.counters = dalloc<unsigned long long>(8);
    CK(cudaMemset(d.counters, 0, 64));
    CK(hipMemcpyToSymbol(HIP_SYMBOL(g_d), &d, sizeof(Dev)));
    unsigned long long *dnext = dalloc<unsigned long long>(1);
    auto t1 = std::chrono::steady_clock::now();

    // the slot loop
    const int64_t CHECK = 100000000;
    int64_t slotStart = 0, stopAt = -1;
    uint64_t slots = 0;
    int32_t completed = 0, errc = 0;
    WorldMut *dmut = d.w.mut;
    size_t offStop = offsetof(Dev, stopAt);
    int64_t lastApply = 0;
    bool prof = getenv("UB_PROF") != nullptr;
    double pt[5] = {};
    auto tick = [&](int k, std::chrono::steady_clock::time_point &tp) {
        if (!prof) return;
        CK(cudaDeviceSynchronize());
        auto t = std::chrono::steady_clock::now();
        pt[k] += std::chrono::duration<double>(t - tp).count();
        tp = t;
    };
    for (;;) {
        auto tp = std::chrono::steady_clock::now();
        int64_t end = slotStart + S;
        if (stopAt >= 0 && end > stopAt)
            end = stopAt + 1;
        tg_slot<<<1, 32>>>(end);
        tick(0, tp);
        CK(cudaMemcpy(&completed, &dmut->completed, 4, cudaMemcpyDeviceToHost));
        if (completed == hw.ntask && stopAt < 0) {
            // the last completion became visible in this slot
            lastApply = end - 1;
            stopAt = (lastApply / CHECK + 1) * CHECK;
            CK(hipMemcpyToSymbol(HIP_SYMBOL(g_d), &stopAt, 8, offStop));
        }
        node_slot<<<NN, 32>>>(end);
        tick(1, tp);
        late_slot<<<1, 1>>>();
        tick(2, tp);
        ++slots;
        CK(cudaMemcpy(&errc, &dmut->err, 4, cudaMemcpyDeviceToHost));
        if (errc || (stopAt >= 0 && end > stopAt))
            break;
        unsigned long long nx = ~0ull;
        CK(cudaMemcpy(dnext, &nx, 8, cudaMemcpyHostToDevice));
        next_slot<<<(NQ + 127) / 128, 128>>>(dnext);
        CK(cudaMemcpy(&nx, dnext, 8, cudaMemcpyDeviceToHost));
        tick(3, tp);
        int64_t next = (int64_t)nx;
        if (nx == ~0ull || next == INT64_MAX) {
            if (stopAt < 0)
                break;
            next = stopAt;
        }
        slotStart = std::max(end, (next / S) * S);
    }
    CK(cudaDeviceSynchronize());
    auto t2 = std::chrono::steady_clock::now();
    if (prof)
        fprintf(stderr, "twins-ub: prof tg %.3f node %.3f late %.3f next %.3f s\n", pt[0], pt[1], pt[2], pt[3]);
#ifdef UB_NODE_PROF
    {
        std::vector<unsigned long long> cyc(4096);
        CK(hipMemcpyFromSymbol(cyc.data(), HIP_SYMBOL(g_cyc), 8 * 4096));
        unsigned long long mx = 0, sum = 0; int arg = 0;
        for (int i = 0; i < NN && i < 4096; ++i) { sum += cyc[i]; if (cyc[i] > mx) mx = cyc[i], arg = i; }
        std::vector<unsigned long long> ev(4096);
        CK(hipMemcpyFromSymbol(ev.data(), HIP_SYMBOL(g_evn2), 8 * 4096));
        unsigned long long sc[8];
        CK(hipMemcpyFromSymbol(sc, HIP_SYMBOL(g_sect), 64));
        unsigned long long hmax, hsum;
        CK(hipMemcpyFromSymbol(&hmax, HIP_SYMBOL(g_hmax), 8));
        CK(hipMemcpyFromSymbol(&hsum, HIP_SYMBOL(g_hsum), 8));
        fprintf(stderr, "twins-ub: heap max %llu, mean at insert %.1f\n", hmax, (double)hsum / 144843.0);
        fprintf(stderr, "twins-ub: sections: heap_push %llu, heap_pop %llu, dispatch(incl. pushes) %llu\n", sc[0], sc[1], sc[2]);
        fprintf(stderr, "twins-ub: node cycles: max %llu (node %d, %s, %llu events, %.0f cycles/event), sum %llu, nodes %d\n", mx, arg, H.node[arg].sw ? "switch" : "host", ev[arg], (double)mx / std::max(1ull, ev[arg]), sum, NN);
        for (int i = 0; i < NN && i < 4096; i += NN / 8 + 1) fprintf(stderr, "   node %d: %llu cycles, %llu events, %.0f c/e\n", i, cyc[i], ev[i], (double)cyc[i] / std::max(1ull, ev[i]));
    }
#endif
    unsigned long long cnt[8], st[8], tailCnt, trN;
    CK(cudaMemcpy(cnt, d.counters, 64, cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(st, d.cx.stat, 64, cudaMemcpyDeviceToHost));
    if (prof) fprintf(stderr, "twins-ub: ccmp %llu, walks %llu, walk steps %llu\n", st[4], st[2], st[3]);
    CK(cudaMemcpy(&tailCnt, d.cx.cnt, 8, cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(&trN, d.trN, 8, cudaMemcpyDeviceToHost));
    WorldMut fm;
    CK(cudaMemcpy(&fm, dmut, sizeof fm, cudaMemcpyDeviceToHost));
    if (trace) {
        if (trN > d.trCap) {
            fprintf(stderr, "twins-ub: trace buffer overflow (%llu records)\n", trN);
            return 2;
        }
        std::vector<TRec> tr(trN);
        CK(cudaMemcpy(tr.data(), d.tr, sizeof(TRec) * trN, cudaMemcpyDeviceToHost));
        std::stable_sort(tr.begin(), tr.end(), [](const TRec &a, const TRec &b) {
            return a.node != b.node ? a.node < b.node : a.seq < b.seq;
        });
        FILE *fp = fopen((out + ".port").c_str(), "w"), *ft = fopen((out + ".task").c_str(), "w");
        for (auto &r : tr) {
            if (r.kind == 'T' || r.kind == 'R')
                fprintf(fp, "%lld %d %d %c %u\n", (long long)r.ts, r.node, r.a, r.kind, r.size);
            else
                fprintf(ft, "%lld %d %d %c\n", (long long)r.ts, r.node, r.a, r.kind);
        }
        fclose(fp);
        fclose(ft);
    }
    double tset = std::chrono::duration<double>(t1 - t0).count(), trun = std::chrono::duration<double>(t2 - t1).count();
    if (fm.err) {
        fprintf(stderr, "twins-ub: stopped with error %d\n", fm.err);
        return 2;
    }
    fprintf(stderr, "twins-ub: %d/%d tasks, %llu events, %llu slots of %lld ps, end %lld ps; late %llu, repaired %llu, "
                    "conflicts %llu, overflow %llu; undecided %llu, inexact %llu, tail nodes %llu; set-up %.2f s, run %.3f s\n",
            fm.completed, hw.ntask, cnt[0], (unsigned long long)slots, (long long)S, (long long)stopAt, cnt[1], cnt[2],
            cnt[3], cnt[4], st[0], st[1], tailCnt, tset, trun);
    return fm.completed == hw.ntask && cnt[3] == 0 && st[0] == 0 ? 0 : 3;
}
