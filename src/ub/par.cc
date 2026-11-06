// twins-ubpar: model.h under the slot-synchronous parallel engine, run on one CPU thread.
//
// This is the GPU engine's algorithm without the GPU: time advances in slots of S (the shortest
// link delay, at most the dependency visibility delay), and in a slot every node runs its events
// below the slot's end on its own, in ns-3's order by chains (chain.h): its heap holds its local
// events and the arrivals and messages delivered to it. A slot first runs the traffic generator
// (its events are scheduled at least S ahead), then the nodes; what a node sends to another node
// arrives at least S later, except the destroy notice of a TP connection's peer endpoint
// (zero delay in ns-3): it is applied after the slot, and the peer's allocator decisions since that
// instant are replayed without the removed TP (repair); a decision that would differ is counted
// as a conflict (the run is then not exact). See docs/ub/parallel.md.
//
// Usage: twins-ubpar CASE_DIR [--out=PREFIX] [--dep-delay=TIME] [--pool=SCALE]
#include "chain.h"
#include "setup.h"
#include <algorithm>
#include <cstdio>
#include <vector>

using namespace ub;

struct Ent {
    int64_t ts;
    uint64_t seq;     // insertion order: breaks ties of equal chains (twin events)
    Chain c;
    Ev ev;
    Pkt pk;           // E_RECV: the frame
};

struct SelLog {       // an allocator decision on a port with a TP awaiting a late destroy notice
    int64_t ts;
    Chain c;
    int32_t port, vi;
    uint32_t start, chosen, slots;
    uint64_t cand[4]; // candidate slots (bit per slot)
};
struct LateMsg {
    int32_t node;
    int64_t ts;
    Chain c;
    Ev ev;
};

struct ParEngine;
typedef Model<ParEngine> M_t;

struct NodeQ {
    std::vector<Ent> pool;
    std::vector<int32_t> freeIdx, heap;
    std::vector<Ent> inbox;
    std::vector<SelLog> sel;
};

struct ParEngine {
    HostWorld &H;
    World &w;
    ChainCtx cx{};
    std::vector<TailNode> tailv;
    std::vector<unsigned long long> memov, walkv;
    unsigned long long stat[4] = {};
    uint64_t tailCnt = 0;
    std::vector<uint32_t> rootRank;
    std::vector<NodeQ> nq;      // nnode + 1 (the traffic generator last)
    int tgq;
    // the running event
    int32_t curNode = -1;
    int64_t now_ = 0;
    Chain cur{};
    uint64_t ord = 0;
    Chain lastChild{};          // at_node_twin: the chain and time of the last at_node
    int64_t lastChildTs = 0;
    uint64_t seqc = 0;
    bool rootBatch = false;
    uint32_t nextRoot = 1;
    std::vector<LateMsg> late;
    M_t *M = nullptr;
    FILE *fport = nullptr, *ftask = nullptr;
    uint64_t events = 0, conflicts = 0, repairs = 0, lateMsgs = 0;

    ParEngine(HostWorld &h) : H(h), w(h.w)
    {
        tailv.resize(1 << 22);
        memov.assign(1 << 20, 0);
        walkv.assign(1 << 18, 0);
        cx.tail = tailv.data();
        cx.mask = tailv.size() - 1;
        cx.cnt = &tailCnt;
        rootRank.resize(w.ntask + 1);
        for (int i = 0; i <= w.ntask; ++i)
            rootRank[i] = i;
        cx.rootRank = rootRank.data();
        cx.memo = memov.data();
        cx.memoMask = memov.size() - 1;
        cx.walk = walkv.data();
        cx.walkMask = walkv.size() - 1;
        cx.stat = stat;
        nq.resize(w.nnode + 1);
        tgq = w.nnode;
    }
    bool ent_before(const Ent &a, const Ent &b)
    {
        if (a.ts != b.ts)
            return a.ts < b.ts;
        bool inexact;
        int c = ccmp(cx, a.c, b.c, &inexact);
        if (inexact)
            stat[1]++;
        if (c == 0) {
            // equal chains: twin events, in insertion order; anything else is undecided
            if (memcmp(&a.c, &b.c, sizeof(Chain)) != 0)
                stat[0]++;
            return a.seq < b.seq;
        }
        return c < 0;
    }
    // heap of pool indices
    void heap_push(NodeQ &q, const Ent &e)
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
        q.pool[i].seq = seqc++;
        q.heap.push_back(i);
        size_t k = q.heap.size() - 1;
        while (k > 0) {
            size_t p = (k - 1) / 2;
            if (!ent_before(q.pool[q.heap[k]], q.pool[q.heap[p]]))
                break;
            std::swap(q.heap[k], q.heap[p]);
            k = p;
        }
    }
    int32_t heap_pop(NodeQ &q)
    {
        int32_t top = q.heap[0];
        q.heap[0] = q.heap.back();
        q.heap.pop_back();
        size_t k = 0, n = q.heap.size();
        for (;;) {
            size_t l = 2 * k + 1, r = l + 1, m = k;
            if (l < n && ent_before(q.pool[q.heap[l]], q.pool[q.heap[m]])) m = l;
            if (r < n && ent_before(q.pool[q.heap[r]], q.pool[q.heap[m]])) m = r;
            if (m == k)
                break;
            std::swap(q.heap[k], q.heap[m]);
            k = m;
        }
        return top;
    }
    // ---- the engine interface of model.h
    int64_t now() const { return now_; }
    void child(Chain &out, int64_t delay)
    {
        cpush(cx, &out, ccode(delay, ord++), cur);
    }
    void sched(Node &n, int64_t delay, Ev ev)
    {
        Ent e;
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = ev;
        heap_push(nq[n.id], e);
    }
    void link(Node &n, int32_t p, const Pkt &pk, int64_t delay)
    {
        Port &pt = port_of(w, n, p);
        Ent e;
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = Ev{E_RECV, pt.peerPort, 0, 0, 0};
        e.pk = pk;
        nq[pt.peerNode].inbox.push_back(e);
    }
    void tg(Node &, int64_t delay, Ev ev)
    {
        Ent e;
        e.ts = now_ + delay;
        child(e.c, delay);
        e.ev = ev;
        nq[tgq].inbox.push_back(e);
    }
    void at_node(int32_t node, int64_t delay, Ev ev)
    {
        Ent e;
        e.ts = now_ + delay;
        if (rootBatch) {
            Chain r;
            chain_root(&r, nextRoot++);
            cpush(cx, &e.c, ccode(delay, 0), r);
        } else
            child(e.c, delay);
        e.ev = ev;
        lastChild = e.c;
        lastChildTs = e.ts;
        nq[node].inbox.push_back(e);
    }
    void at_node_twin(int32_t node, Ev ev)
    {
        Ent e;
        e.ts = lastChildTs;
        e.c = lastChild;
        e.ev = ev;
        nq[node].inbox.push_back(e);
    }
    void lc_peer(int32_t, int32_t, int) {}   // the traffic generator sent the peer its part
    void lc_destroy2(int32_t node, int32_t ci)
    {
        late.push_back(LateMsg{node, now_, cur, Ev{E_LCDESTROY2, ci, 0, 0, 0}});
        ++lateMsgs;
    }
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
        L.c = cur;
        L.port = p;
        L.vi = vi;
        L.start = start;
        L.chosen = chosen;
        L.slots = slots;
        // the candidates as they are now (select_next has just evaluated them)
        Port &pt = port_of(w, n, p);
        for (int32_t in = 0; in < n.P; ++in) {
            int32_t q = (int32_t)(&voq_of(w, n, p, vi, in) - w.voq);
            if (w.voq[q].n && !M->fc_limited(n, pt, q) && in < 256)
                L.cand[in >> 6] |= 1ull << (in & 63);
        }
        const int32_t *tl = w.tplist + pt.tplist + vi * n.ntp;
        for (int32_t o = 0; o < pt.tpcnt[vi]; ++o) {
            int32_t q = iq_tp(tl[o]);
            uint32_t slot = n.P + o;
            if (!M->iq_empty(q) && !M->iq_limited(q) && !M->fc_limited(n, pt, q) && slot < 256)
                L.cand[slot >> 6] |= 1ull << (slot & 63);
        }
        nq[n.id].sel.push_back(L);
    }
    int32_t route_candidates(Node &n, uint32_t dip, int32_t inPort, int32_t *cand)
    {
        return ub_route(w, n.id, dip, inPort, cand);
    }
    void trace_tx(Node &n, int32_t p, const Pkt &pk)
    {
        if (fport)
            fprintf(fport, "%lld %d %d T %u\n", (long long)now_, n.id, p, pk.size);
    }
    void trace_rx(Node &n, int32_t p, const Pkt &pk)
    {
        if (fport)
            fprintf(fport, "%lld %d %d R %u\n", (long long)now_, n.id, p, pk.size);
    }
    void trace_task(Node &n, const TaskG &t, char c)
    {
        if (ftask)
            fprintf(ftask, "%lld %d %d %c\n", (long long)now_, n.id, t.id, c);
    }
    void trace_tp_destroy(Node &, const Tp &) {}
    void trace_conn_destroy(int32_t, int32_t) {}

    // ---- the late destroy notice of a peer endpoint, with the allocator repair
    void apply_late(TrafficGen<ParEngine> &, const LateMsg &m)
    {
        Node &n = w.node[m.node];
        int32_t ci = m.ev.a;
        int32_t ti = w.conn[ci].ep[2];
        if (ti < 0 || w.tp[ti].dead) {
            // never materialized (or gone): nothing on the allocator
            curNode = n.id;
            now_ = m.ts;
            cur = m.c;
            M->lc_destroy2(ci);
            return;
        }
        Tp &t = w.tp[ti];
        Port &pt = port_of(w, n, t.port);
        // the TP's slot, and the decisions on its port and VL after the notice's place
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
        for (auto &L : nq[n.id].sel)
            if (L.port == t.port && L.vi == t.vi) {
                Ent le;
                le.ts = L.ts;
                le.c = L.c;
                le.seq = 0;
                if (ent_before(probe, le))
                    after.push_back(&L);
            }
        bool quiescent = M->tp_quiescent(ti);
        if (!quiescent || after.empty()) {
            // removal now is the same as removal at its instant (or later, by the TP itself)
            curNode = n.id;
            now_ = m.ts;
            cur = m.c;
            M->lc_destroy2(ci);
            return;
        }
        // replay the decisions as ns-3 made them without the TP
        ++repairs;
        uint32_t r = after[0]->start;
        bool ok = true;
        for (SelLog *L : after) {
            if (L->start != r && L != after[0]) {
                // our own start already followed our numbering; the replayed one is r
            }
            uint32_t slotsP = L->slots - 1;
            // candidates in ns-3's numbering
            int32_t best = -1, wrap = -1;
            for (uint32_t s = 0; s < L->slots && s < 256; ++s) {
                if (!(L->cand[s >> 6] >> (s & 63) & 1))
                    continue;
                if (s == sX) { ok = false; break; }   // the TP itself was a candidate
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
        if (!ok) {
            ++conflicts;
        }
        // remove the TP and set the round-robin index ns-3 would have
        curNode = n.id;
        now_ = m.ts;
        cur = m.c;
        M->lc_destroy2(ci);
        if (ok)
            pt.rrIdx[n.vlOf[t.vi]] = r;
    }
};

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: twins-ubpar CASE_DIR [--out=PREFIX] [--dep-delay=TIME] [--pool=SCALE]\n");
        return 1;
    }
    std::string dir = argv[1], out;
    int64_t depDelay = 100000;
    double pool = 1.0;
    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        if (a.rfind("--out=", 0) == 0)
            out = a.substr(6);
        else if (a.rfind("--dep-delay=", 0) == 0)
            depDelay = parse_time_ps(a.substr(12));
        else if (a.rfind("--pool=", 0) == 0)
            pool = atof(a.c_str() + 7);
        else {
            fprintf(stderr, "twins-ubpar: unknown option %s\n", a.c_str());
            return 1;
        }
    }
    Case C;
    HostWorld H;
    try {
        load_case(dir, C);
    } catch (std::exception &e) {
        fprintf(stderr, "twins-ubpar: %s\n", e.what());
        return 2;
    }
    std::string err = build_world(C, H, depDelay, pool);
    if (!err.empty()) {
        fprintf(stderr, "twins-ubpar: %s\n", err.c_str());
        return 2;
    }
    World &w = H.w;
    ParEngine E(H);
    Model<ParEngine> M(E, w);
    E.M = &M;
    TrafficGen<ParEngine> G(E, w);
    if (!out.empty()) {
        E.fport = fopen((out + ".port").c_str(), "w");
        E.ftask = fopen((out + ".task").c_str(), "w");
    }
    // slot length
    int64_t S = depDelay;
    for (auto &l : C.links)
        S = std::min(S, l.delay_ps);
    if (S <= 0) {
        fprintf(stderr, "twins-ubpar: a zero link delay leaves no lookahead\n");
        return 2;
    }
    // tasks at load: roots in start order
    E.rootBatch = true;
    E.now_ = 0;
    G.schedule_ready();
    E.rootBatch = false;
    // the stop instant: CheckExampleProcess runs every 100 us; at the first one after the last
    // completion the run stops, after the events of that instant scheduled before it (lead > 0)
    const int64_t CHECK = 100000000;
    int64_t stopAt = -1;
    int64_t slotStart = 0;
    uint64_t slots = 0;
    auto drain = [&](NodeQ &q) {
        for (auto &e : q.inbox)
            E.heap_push(q, e);
        q.inbox.clear();
    };
    auto run_node = [&](int32_t id, int64_t end) {
        NodeQ &q = E.nq[id];
        drain(q);
        while (!q.heap.empty()) {
            const Ent &top = q.pool[q.heap[0]];
            if (top.ts >= end)
                break;
            if (stopAt >= 0 && top.ts == stopAt) {
                // at the stop instant only events scheduled before it run (ns-3: uid < Stop's)
                uint64_t lead = (top.c.r[0].code >> ORD_BITS) - 1;
                if (lead == 0)
                    break;
            }
            int32_t i = E.heap_pop(q);
            Ent e = q.pool[i];
            q.freeIdx.push_back(i);
            E.curNode = id;
            E.now_ = e.ts;
            E.cur = e.c;
            E.ord = 0;
            ++E.events;
            if (id == E.tgq)
                G.apply(e.ev.a);
            else if (e.ev.kind == E_RECV)
                M.receive(w.node[id], e.ev.a, e.pk);
            else
                M.dispatch(w.node[id], e.ev);
            if (w.mut->err)
                return;
        }
    };
    for (;;) {
        int64_t end = slotStart + S;
        if (stopAt >= 0 && end > stopAt)
            end = stopAt + 1;
        run_node(E.tgq, end);
        if (w.mut->completed == w.ntask && stopAt < 0) {
            int64_t last = E.now_;
            stopAt = (last / CHECK + 1) * CHECK;
        }
        for (int32_t i = 0; i < w.nnode && !w.mut->err; ++i)
            run_node(i, end);
        // late destroy notices, in their order
        std::sort(E.late.begin(), E.late.end(), [&](const LateMsg &a, const LateMsg &b) {
            Ent x, y;
            x.ts = a.ts, x.c = a.c, x.seq = 0;
            y.ts = b.ts, y.c = b.c, y.seq = 1;
            return E.ent_before(x, y);
        });
        for (auto &m : E.late)
            E.apply_late(G, m);
        E.late.clear();
        for (auto &q : E.nq)
            q.sel.clear();
        ++slots;
        if (w.mut->err || (stopAt >= 0 && end > stopAt))
            break;
        // next slot: skip idle time
        int64_t next = INT64_MAX;
        for (auto &q : E.nq) {
            for (auto &e : q.inbox) next = std::min(next, e.ts);
            if (!q.heap.empty()) next = std::min(next, q.pool[q.heap[0]].ts);
        }
        if (next == INT64_MAX) {
            if (stopAt < 0)
                break;
            next = stopAt;
        }
        slotStart = std::max(end, (next / S) * S);
    }
    if (E.fport) fclose(E.fport);
    if (E.ftask) fclose(E.ftask);
    if (w.mut->err) {
        fprintf(stderr, "twins-ubpar: stopped with error %d\n", w.mut->err);
        return 2;
    }
    fprintf(stderr, "twins-ubpar: %d/%d tasks, %llu events, %llu slots of %lld ps, end %lld ps; late %llu, repaired %llu, "
                    "conflicts %llu; undecided %llu, inexact %llu, tail nodes %llu\n",
            w.mut->completed, w.ntask, (unsigned long long)E.events, (unsigned long long)slots, (long long)S,
            (long long)stopAt, (unsigned long long)E.lateMsgs, (unsigned long long)E.repairs,
            (unsigned long long)E.conflicts, E.stat[0], E.stat[1], (unsigned long long)E.tailCnt);
    return w.mut->completed == w.ntask && E.conflicts == 0 ? 0 : 3;
}
