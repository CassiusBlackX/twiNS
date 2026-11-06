// twins-ubmt: the slot-synchronous engine of par.cc on several CPU threads.
//
// In a slot, thread 0 runs the traffic generator, then every thread runs its share of the nodes
// (each node's events in chain order, as in par.cc), then thread 0 applies the late destroy
// notices (with the allocator repair) and finds the next slot. Arrivals and messages go into the
// receiving node's inbox (a spin lock per inbox); they are always for a later slot, except the
// traffic generator's, which come before the nodes run. Results do not depend on the thread
// count. Zero-delay events of a node run from a FIFO (see gpu.cu: in ns-3 they follow the
// instant's earlier-scheduled events, in the order they were scheduled).
//
// Usage: twins-ubmt CASE_DIR [--threads=N] [--out=PREFIX] [--dep-delay=TIME] [--pool=SCALE]
#include "chain.h"
#include "setup.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <thread>
#include <vector>
#if defined(__x86_64__)
#include <immintrin.h>
#define CPU_RELAX() _mm_pause()
#else
#define CPU_RELAX()
#endif

using namespace ub;

struct Ent {
    int64_t ts;
    uint64_t seq;
    Chain c;
    Ev ev;
    Pkt pk;
};
struct SelLog {
    int64_t ts;
    Chain c;
    int32_t port, vi;
    uint32_t start, chosen, slots;
    uint64_t cand[4];
};
struct LateMsg {
    int32_t node;
    int64_t ts;
    Chain c;
    Ev ev;
};
struct TRec {
    int64_t ts;
    int32_t a;
    uint32_t size;
    char kind;
};

struct alignas(64) NodeQ {
    std::vector<Ent> pool;
    std::vector<int32_t> freeIdx, heap, fifo;
    size_t fhead = 0;
    std::vector<Ent> inbox;
    std::atomic_flag lock = ATOMIC_FLAG_INIT;
    uint64_t seqc = 0;
    std::vector<SelLog> sel;
    std::vector<TRec> tr;
};

struct Shared {
    HostWorld &H;
    World &w;
    ChainCtx cx{};
    std::vector<TailNode> tailv;
    std::vector<unsigned long long> memov, walkv;
    unsigned long long stat[8] = {};
    uint64_t tailCnt = 0;
    std::vector<uint32_t> rootRank;
    std::vector<NodeQ> nq;
    int tgq;
    int64_t stopAt = -1;
    bool trace = false;
    Shared(HostWorld &h) : H(h), w(h.w), nq(h.w.nnode + 1)
    {
        tailv.resize(1 << 24);
        memov.assign(1 << 22, 0);
        walkv.assign(1 << 20, 0);
        cx.tail = tailv.data();
        cx.mask = tailv.size() - 1;
        cx.cnt = &tailCnt;
        rootRank.resize(w.ntask + 2);
        for (size_t i = 0; i < rootRank.size(); ++i) rootRank[i] = (uint32_t)i;
        cx.rootRank = rootRank.data();
        cx.memo = memov.data();
        cx.memoMask = memov.size() - 1;
        cx.walk = walkv.data();
        cx.walkMask = walkv.size() - 1;
        cx.stat = stat;
        tgq = w.nnode;
    }
};

static bool ent_before(Shared &S, const Ent &a, const Ent &b)
{
    if (a.ts != b.ts)
        return a.ts < b.ts;
    bool inexact;
    int c = ccmp(S.cx, a.c, b.c, &inexact);
    if (inexact)
        UB_ATOMIC_ADD(&S.stat[1], 1);
    if (c == 0) {
        if (memcmp(&a.c, &b.c, sizeof(Chain)) != 0)
            UB_ATOMIC_ADD(&S.stat[0], 1);
        return a.seq < b.seq;
    }
    return c < 0;
}

static int32_t pool_put(NodeQ &q, const Ent &e)
{
    int32_t i;
    if (!q.freeIdx.empty()) {
        i = q.freeIdx.back();
        q.freeIdx.pop_back();
        q.pool[i] = e;
    } else {
        i = (int32_t)q.pool.size();
        q.pool.push_back(e);
    }
    q.pool[i].seq = q.seqc++;
    return i;
}
static void heap_insert(Shared &S, NodeQ &q, int32_t i)
{
    q.heap.push_back(i);
    size_t k = q.heap.size() - 1;
    while (k > 0) {
        size_t p = (k - 1) / 2;
        if (!ent_before(S, q.pool[q.heap[k]], q.pool[q.heap[p]]))
            break;
        std::swap(q.heap[k], q.heap[p]);
        k = p;
    }
}
static int32_t heap_pop(Shared &S, NodeQ &q)
{
    int32_t top = q.heap[0];
    q.heap[0] = q.heap.back();
    q.heap.pop_back();
    size_t k = 0, n = q.heap.size();
    for (;;) {
        size_t l = 2 * k + 1, r = l + 1, m = k;
        if (l < n && ent_before(S, q.pool[q.heap[l]], q.pool[q.heap[m]])) m = l;
        if (r < n && ent_before(S, q.pool[q.heap[r]], q.pool[q.heap[m]])) m = r;
        if (m == k)
            break;
        std::swap(q.heap[k], q.heap[m]);
        k = m;
    }
    return top;
}
static Ent &inbox_put(Shared &S, int32_t node)
{
    NodeQ &q = S.nq[node];
    while (q.lock.test_and_set(std::memory_order_acquire))
        CPU_RELAX();
    q.inbox.emplace_back();
    Ent &e = q.inbox.back();
    // (filled by the caller before the lock is released: see inbox_done)
    return e;
}
static void inbox_done(Shared &S, int32_t node) { S.nq[node].lock.clear(std::memory_order_release); }

struct TEngine;
typedef Model<TEngine> M_t;

struct TEngine {
    Shared &S;
    World &w;
    int tid;
    int32_t curNode = -1;
    int64_t now_ = 0;
    const Chain *cur = nullptr;
    uint64_t ord = 0;
    Chain lastChild{};
    int64_t lastChildTs = 0;
    M_t *M = nullptr;
    std::vector<LateMsg> late;
    uint64_t events = 0;
    TEngine(Shared &s, int t) : S(s), w(s.w), tid(t) {}
    int64_t now() const { return now_; }
    void child(Chain &out, int64_t delay) { cpush(S.cx, &out, ccode(delay, ord++), *cur); }
    void sched(Node &n, int64_t delay, Ev ev)
    {
        NodeQ &q = S.nq[n.id];
        Ent e;
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = ev;
        int32_t i = pool_put(q, e);
        if (delay == 0)
            q.fifo.push_back(i);
        else
            heap_insert(S, q, i);
    }
    void link(Node &n, int32_t p, const Pkt &pk, int64_t delay)
    {
        Port &pt = port_of(w, n, p);
        Ent &e = inbox_put(S, pt.peerNode);
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = Ev{E_RECV, pt.peerPort, 0, 0, 0};
        e.pk = pk;
        inbox_done(S, pt.peerNode);
    }
    void tg(Node &, int64_t delay, Ev ev)
    {
        Ent &e = inbox_put(S, S.tgq);
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = ev;
        inbox_done(S, S.tgq);
    }
    void at_node(int32_t node, int64_t delay, Ev ev)
    {
        Ent &e = inbox_put(S, node);
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = ev;
        lastChild = e.c;
        lastChildTs = e.ts;
        inbox_done(S, node);
    }
    void at_node_twin(int32_t node, Ev ev)
    {
        Ent &e = inbox_put(S, node);
        e.ts = lastChildTs;
        e.c = lastChild;
        e.ev = ev;
        inbox_done(S, node);
    }
    void lc_peer(int32_t, int32_t, int) {}
    void lc_destroy2(int32_t node, int32_t ci) { late.push_back(LateMsg{node, now_, *cur, Ev{E_LCDESTROY2, ci, 0, 0, 0}}); }
    bool port_has_held_tp(Node &n, int32_t p, int32_t vi)
    {
        Port &pt = port_of(w, n, p);
        const int32_t *tl = w.tplist + pt.tplist + vi * n.ntp;
        for (int32_t o = 0; o < pt.tpcnt[vi]; ++o)
            if (w.tp[tl[o]].drainHold)
                return true;
        return false;
    }
    void log_select(Node &n, int32_t p, int32_t vi, uint32_t start, uint32_t chosen, uint32_t slots)
    {
        if (!port_has_held_tp(n, p, vi))
            return;
        SelLog L{};
        L.ts = now_;
        L.c = *cur;
        L.port = p;
        L.vi = vi;
        L.start = start;
        L.chosen = chosen;
        L.slots = slots;
        Port &pt = port_of(w, n, p);
        for (uint64_t m = pt.vmask[vi]; m; m &= m - 1) {
            int32_t in = __builtin_ctzll(m);
            int32_t q = (int32_t)(&voq_of(w, n, p, vi, in) - w.voq);
            if (!M->fc_limited(n, pt, q))
                L.cand[in >> 6] |= 1ull << (in & 63);
        }
        const int32_t *tl = w.tplist + pt.tplist + vi * n.ntp;
        for (int32_t o = 0; o < pt.tpcnt[vi]; ++o) {
            int32_t q = iq_tp(tl[o]);
            uint32_t slot = n.P + o;
            if (!M->iq_empty(q) && !M->iq_limited(q) && !M->fc_limited(n, pt, q) && slot < 256)
                L.cand[slot >> 6] |= 1ull << (slot & 63);
        }
        S.nq[n.id].sel.push_back(L);
    }
    int32_t route_candidates(Node &n, uint32_t dip, int32_t inPort, int32_t *cand)
    {
        int32_t dn = ip_node(dip);
        uint32_t lastb = dip & 0xff;
        int32_t dp = lastb ? (int32_t)lastb - 1 : 0;
        size_t key = ((size_t)n.id * w.nnode + dn) * HostWorld::MAXHP + dp;
        int32_t c = 0;
        for (int32_t i = 0; i < S.H.rtCnt[key]; ++i) {
            int32_t p = S.H.rtPorts[S.H.rtOff[key] + i];
            if (p != inPort)
                cand[c++] = p;
        }
        return c;
    }
    void trace_tx(Node &n, int32_t p, const Pkt &pk)
    {
        if (S.trace) S.nq[n.id].tr.push_back(TRec{now_, p, pk.size, 'T'});
    }
    void trace_rx(Node &n, int32_t p, const Pkt &pk)
    {
        if (S.trace) S.nq[n.id].tr.push_back(TRec{now_, p, pk.size, 'R'});
    }
    void trace_task(Node &n, const TaskG &t, char c)
    {
        if (S.trace) S.nq[n.id].tr.push_back(TRec{now_, t.id, 0, c});
    }
    void trace_tp_destroy(Node &, const Tp &) {}
    void trace_conn_destroy(int32_t, int32_t) {}

    // one node's (or the traffic generator's) events below end
    void run_node(int32_t id, int64_t end, TrafficGen<TEngine> &G)
    {
        NodeQ &q = S.nq[id];
        // take the inbox under its lock: neighbors on other threads append to it meanwhile (for
        // later slots, so whatever arrives after the swap waits for the next slot)
        std::vector<Ent> in;
        while (q.lock.test_and_set(std::memory_order_acquire))
            CPU_RELAX();
        in.swap(q.inbox);
        q.lock.clear(std::memory_order_release);
        for (auto &e : in)
            heap_insert(S, q, pool_put(q, e));
        for (;;) {
            int32_t i;
            if (q.fhead < q.fifo.size()) {
                int32_t fi = q.fifo[q.fhead];
                const Ent &fe = q.pool[fi];
                if (S.stopAt >= 0 && fe.ts == S.stopAt)
                    break;
                bool heapFirst = false;
                if (!q.heap.empty() && q.pool[q.heap[0]].ts == fe.ts) {
                    const Ent &he = q.pool[q.heap[0]];
                    heapFirst = ((he.c.r[0].code >> ORD_BITS) - 1) > 0 || ent_before(S, he, fe);
                }
                if (heapFirst)
                    i = heap_pop(S, q);
                else {
                    i = fi;
                    if (++q.fhead == q.fifo.size()) {
                        q.fifo.clear();
                        q.fhead = 0;
                    }
                }
            } else {
                if (q.heap.empty())
                    break;
                const Ent &top = q.pool[q.heap[0]];
                if (top.ts >= end)
                    break;
                if (S.stopAt >= 0 && top.ts == S.stopAt && ((top.c.r[0].code >> ORD_BITS) - 1) == 0)
                    break;
                i = heap_pop(S, q);
            }
            // the event runs from a copy: its handlers may grow the pool
            Ent e = q.pool[i];
            curNode = id;
            now_ = e.ts;
            cur = &e.c;
            ord = 0;
            ++events;
            if (id == S.tgq)
                G.apply(e.ev.a);
            else if (e.ev.kind == E_RECV)
                M->receive(w.node[id], e.ev.a, e.pk);
            else
                M->dispatch(w.node[id], e.ev);
            q.freeIdx.push_back(i);
            if (w.mut->err)
                return;
        }
    }
    int64_t next_time(int32_t id)
    {
        NodeQ &q = S.nq[id];
        int64_t m = INT64_MAX;
        if (!q.heap.empty()) m = q.pool[q.heap[0]].ts;
        if (q.fhead < q.fifo.size()) m = std::min(m, q.pool[q.fifo[q.fhead]].ts);
        for (auto &e : q.inbox) m = std::min(m, e.ts);
        return m;
    }
};

// the late destroy notice of a peer endpoint, with the allocator repair (par.cc apply_late)
static void apply_late(Shared &S, TEngine &E, const LateMsg &m, uint64_t &repairs, uint64_t &conflicts)
{
    World &w = S.w;
    M_t &M = *E.M;
    Node &n = w.node[m.node];
    int32_t ci = m.ev.a;
    int32_t ti = w.conn[ci].ep[2];
    E.curNode = n.id;
    E.now_ = m.ts;
    E.cur = &m.c;
    E.ord = 0;
    if (ti < 0 || w.tp[ti].dead) {
        M.lc_destroy2(ci);
        return;
    }
    Tp &t = w.tp[ti];
    Port &pt = port_of(w, n, t.port);
    const int32_t *tl = w.tplist + pt.tplist + t.vi * n.ntp;
    int32_t ordX = -1;
    for (int32_t o = 0; o < pt.tpcnt[t.vi]; ++o)
        if (tl[o] == ti)
            ordX = o;
    uint32_t sX = n.P + ordX;
    Ent probe;
    probe.ts = m.ts;
    probe.c = m.c;
    probe.seq = ~0ull;
    std::vector<SelLog *> after;
    for (auto &L : S.nq[n.id].sel)
        if (L.port == t.port && L.vi == t.vi) {
            Ent le;
            le.ts = L.ts;
            le.c = L.c;
            le.seq = 0;
            if (ent_before(S, probe, le))
                after.push_back(&L);
        }
    if (!M.tp_quiescent(ti) || after.empty()) {
        M.lc_destroy2(ci);
        return;
    }
    ++repairs;
    uint32_t r = after[0]->start;
    bool ok = true;
    for (SelLog *L : after) {
        uint32_t slotsP = L->slots - 1;
        int32_t best = -1, wrap = -1;
        for (uint32_t s = 0; s < L->slots && s < 256; ++s) {
            if (!(L->cand[s >> 6] >> (s & 63) & 1))
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
        int32_t ours = L->chosen == ~0u ? -1 : (int32_t)(L->chosen > sX ? L->chosen - 1 : L->chosen);
        if (chosenP != ours) {
            ok = false;
            break;
        }
        if (chosenP >= 0)
            r = (uint32_t)(chosenP + 1) % slotsP;
    }
    if (!ok)
        ++conflicts;
    M.lc_destroy2(ci);
    if (ok)
        pt.rrIdx[n.vlOf[t.vi]] = r;
}

struct SpinBarrier {
    std::atomic<int> count{0};
    std::atomic<int> gen{0};
    int n;
    explicit SpinBarrier(int n_) : n(n_) {}
    void wait()
    {
        int g = gen.load(std::memory_order_acquire);
        if (count.fetch_add(1, std::memory_order_acq_rel) == n - 1) {
            count.store(0, std::memory_order_relaxed);
            gen.store(g + 1, std::memory_order_release);
        } else
            while (gen.load(std::memory_order_acquire) == g)
                CPU_RELAX();
    }
};

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: twins-ubmt CASE_DIR [--threads=N] [--out=PREFIX] [--dep-delay=TIME] [--pool=SCALE]\n");
        return 1;
    }
    auto t0 = std::chrono::steady_clock::now();
    std::string dir = argv[1], out;
    int64_t depDelay = 100000;
    double pool = 1.0;
    int T = (int)std::thread::hardware_concurrency();
    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        if (a.rfind("--out=", 0) == 0)
            out = a.substr(6);
        else if (a.rfind("--dep-delay=", 0) == 0)
            depDelay = parse_time_ps(a.substr(12));
        else if (a.rfind("--pool=", 0) == 0)
            pool = atof(a.c_str() + 7);
        else if (a.rfind("--threads=", 0) == 0)
            T = atoi(a.c_str() + 10);
        else {
            fprintf(stderr, "twins-ubmt: unknown option %s\n", a.c_str());
            return 1;
        }
    }
    if (T < 1) T = 1;
    Case C;
    HostWorld H;
    try {
        load_case(dir, C);
    } catch (std::exception &e) {
        fprintf(stderr, "twins-ubmt: %s\n", e.what());
        return 2;
    }
    std::string err = build_world(C, H, depDelay, pool);
    if (!err.empty()) {
        fprintf(stderr, "twins-ubmt: %s\n", err.c_str());
        return 2;
    }
    World &w = H.w;
    Shared S(H);
    S.trace = !out.empty();
    int64_t SL = depDelay;
    for (auto &l : C.links)
        SL = std::min(SL, l.delay_ps);
    if (SL <= 0) {
        fprintf(stderr, "twins-ubmt: a zero link delay leaves no lookahead\n");
        return 2;
    }
    // the tasks at load: roots in start order (thread 0's engine)
    std::vector<TEngine *> engines;
    std::vector<M_t *> models;
    std::vector<TrafficGen<TEngine> *> gens;
    for (int t = 0; t < T; ++t) {
        engines.push_back(new TEngine(S, t));
        models.push_back(new M_t(*engines[t], w));
        engines[t]->M = models[t];
        gens.push_back(new TrafficGen<TEngine>(*engines[t], w));
    }
    {
        uint32_t nroot = 0;
        for (int32_t k = 0; k < H.mut.nready; ++k) {
            int32_t ti = H.ready[k];
            TaskG &t = H.task[ti];
            if (t.state != 1)
                continue;
            t.state = 2;
            Ent e{};
            e.ts = t.delay;
            Chain r;
            chain_root(&r, ++nroot);
            cpush(S.cx, &e.c, ccode(t.delay, 0), r);
            e.ev = Ev{E_START, ti, 0, 0, 0};
            S.nq[t.src].inbox.push_back(e);
        }
        H.mut.nready = 0;
    }
    // nodes by thread: interleaved
    std::vector<std::vector<int32_t>> mine(T);
    for (int32_t i = 0; i < w.nnode; ++i)
        mine[i % T].push_back(i);
    const int64_t CHECK = 100000000;
    SpinBarrier bar(T);
    std::atomic<bool> done{false};
    int64_t slotStart = 0, slotEnd = SL;
    uint64_t slots = 0, repairs = 0, conflicts = 0, lateN = 0;
    std::vector<int64_t> nextOf(T, INT64_MAX);
    auto t1 = std::chrono::steady_clock::now();
    auto worker = [&](int t) {
        TEngine &E = *engines[t];
        for (;;) {
            int64_t end = slotEnd;
            if (t == 0) {
                E.run_node(S.tgq, end, *gens[0]);
                if (w.mut->completed == w.ntask && S.stopAt < 0)
                    S.stopAt = (E.now_ / CHECK + 1) * CHECK;
            }
            bar.wait();
            if (S.stopAt >= 0 && end > S.stopAt)
                end = S.stopAt + 1;
            for (int32_t id : mine[t]) {
                E.run_node(id, end, *gens[t]);
                if (w.mut->err)
                    break;
            }
            bar.wait();
            int64_t nx = INT64_MAX;
            for (int32_t id : mine[t])
                nx = std::min(nx, E.next_time(id));
            if (t == 0)
                nx = std::min(nx, E.next_time(S.tgq));
            nextOf[t] = nx;
            bar.wait();
            if (t == 0) {
                std::vector<LateMsg> late;
                for (auto *en : engines) {
                    late.insert(late.end(), en->late.begin(), en->late.end());
                    en->late.clear();
                }
                lateN += late.size();
                std::sort(late.begin(), late.end(), [&](const LateMsg &a, const LateMsg &b) {
                    Ent x, y;
                    x.ts = a.ts, x.c = a.c, x.seq = 0;
                    y.ts = b.ts, y.c = b.c, y.seq = 1;
                    return ent_before(S, x, y);
                });
                for (auto &m : late)
                    apply_late(S, E, m, repairs, conflicts);
                for (auto &q : S.nq)
                    q.sel.clear();
                ++slots;
                int64_t next = INT64_MAX;
                for (int k = 0; k < T; ++k) next = std::min(next, nextOf[k]);
                if (getenv("UB_DBG")) { fprintf(stderr, "slot %llu end %lld next", (unsigned long long)slots, (long long)end); for (int k = 0; k < T; ++k) fprintf(stderr, " %lld", (long long)nextOf[k]); fprintf(stderr, "\n"); }
                bool stop = w.mut->err || (S.stopAt >= 0 && end > S.stopAt);
                if (!stop && next == INT64_MAX) {
                    if (S.stopAt < 0)
                        stop = true;
                    else
                        next = S.stopAt;
                }
                if (stop)
                    done.store(true);
                else {
                    slotStart = std::max(end, (next / SL) * SL);
                    slotEnd = slotStart + SL;
                    if (S.stopAt >= 0 && slotEnd > S.stopAt)
                        slotEnd = S.stopAt + 1;
                }
            }
            bar.wait();
            if (done.load())
                return;
        }
    };
    std::vector<std::thread> th;
    for (int t = 1; t < T; ++t)
        th.emplace_back(worker, t);
    worker(0);
    for (auto &x : th)
        x.join();
    auto t2 = std::chrono::steady_clock::now();
    uint64_t events = 0;
    for (auto *en : engines) events += en->events;
    if (S.trace) {
        FILE *fp = fopen((out + ".port").c_str(), "w"), *ft = fopen((out + ".task").c_str(), "w");
        for (int32_t i = 0; i < w.nnode; ++i)
            for (auto &r : S.nq[i].tr) {
                if (r.kind == 'T' || r.kind == 'R')
                    fprintf(fp, "%lld %d %d %c %u\n", (long long)r.ts, i, r.a, r.kind, r.size);
                else
                    fprintf(ft, "%lld %d %d %c\n", (long long)r.ts, i, r.a, r.kind);
            }
        fclose(fp);
        fclose(ft);
    }
    if (w.mut->err) {
        fprintf(stderr, "twins-ubmt: stopped with error %d\n", w.mut->err);
        return 2;
    }
    fprintf(stderr, "twins-ubmt: %d/%d tasks, %llu events, %llu slots of %lld ps, end %lld ps, %d threads; late %llu, "
                    "repaired %llu, conflicts %llu; undecided %llu, inexact %llu; set-up %.2f s, run %.3f s\n",
            w.mut->completed, w.ntask, (unsigned long long)events, (unsigned long long)slots, (long long)SL,
            (long long)S.stopAt, T, (unsigned long long)lateN, (unsigned long long)repairs,
            (unsigned long long)conflicts, S.stat[0], S.stat[1], std::chrono::duration<double>(t1 - t0).count(),
            std::chrono::duration<double>(t2 - t1).count());
    return w.mut->completed == w.ntask && conflicts == 0 && S.stat[0] == 0 ? 0 : 3;
}
