// twins-ubseq: model.h under a sequential engine that orders events exactly like single-threaded
// ns-3 (MapScheduler: time, then uid, uid a counter over the Schedule calls). It is the reference
// for the parallel engines: the same handlers, a different event order mechanism.
//
// Usage: twins-ubseq CASE_DIR [--out=PREFIX] [--dep-delay=TIME] [--pool=SCALE]
//   PREFIX.port : time_ps node port T|R size ...   PREFIX.task : time_ps node task S|C
#include "chain.h"
#include "setup.h"
#include <cstdio>
#include <queue>

using namespace ub;

struct QEv {
    int64_t ts;
    uint64_t uid;
    int32_t node;   // -2: the traffic generator, -1: the runner
    Ev ev;
    int32_t ch;     // --chains: the event's chain (index in SeqEngine::chains), else -1
};
struct QCmp {
    bool operator()(const QEv &a, const QEv &b) const { return a.ts != b.ts ? a.ts > b.ts : a.uid > b.uid; }
};

struct SeqEngine {
    HostWorld &H;
    World &w;
    int64_t now_ = 0;
    uint64_t uid = 1;
    std::priority_queue<QEv, std::vector<QEv>, QCmp> q;
    std::vector<Pkt> frames;
    std::vector<int32_t> freeFrames;
    FILE *fport = nullptr, *ftask = nullptr, *ftp = nullptr;
    uint64_t events = 0;
    // --chains: every event's chain, and a check that chain order agrees with the uid order
    bool chains = false;
    ChainCtx cx{};
    std::vector<TailNode> tailv;
    std::vector<unsigned long long> memov, walkv;
    unsigned long long stat[4] = {};
    uint64_t tailCnt = 0;
    std::vector<uint32_t> rootRank;
    std::vector<Chain> chainPool;
    std::vector<int32_t> chainFree;
    Chain cur{};            // the chain of the event being run
    uint64_t ord = 0;       // events it has scheduled so far
    uint64_t checked = 0, disagree = 0;
    struct Last {
        int64_t ts = -1;
        int32_t ch = -1;
    };
    std::vector<Last> last;   // by node + 2 (TG at 0)

    SeqEngine(HostWorld &h) : H(h), w(h.w) {}
    void init_chains(int ntask)
    {
        chains = true;
        tailv.resize(1 << 22);
        memov.assign(1 << 20, 0);
        walkv.assign(1 << 18, 0);
        cx.tail = tailv.data();
        cx.mask = tailv.size() - 1;
        cx.cnt = &tailCnt;
        rootRank.resize(ntask + 1);
        for (int i = 0; i <= ntask; ++i) rootRank[i] = i;
        cx.rootRank = rootRank.data();
        cx.memo = memov.data();
        cx.memoMask = memov.size() - 1;
        cx.walk = walkv.data();
        cx.walkMask = walkv.size() - 1;
        cx.stat = stat;
        last.resize(w.nnode + 2);
    }
    int32_t chain_new(const Chain &c)
    {
        int32_t i;
        if (!chainFree.empty()) {
            i = chainFree.back();
            chainFree.pop_back();
            chainPool[i] = c;
        } else {
            i = (int32_t)chainPool.size();
            chainPool.push_back(c);
        }
        return i;
    }
    int64_t now() const { return now_; }
    void push(int32_t node, int64_t delay, Ev ev)
    {
        int32_t ch = -1;
        if (chains && node != -1) {
            Chain c;
            cpush(cx, &c, ccode(delay, ord++), cur);
            ch = chain_new(c);
        }
        q.push(QEv{now_ + delay, uid++, node, ev, ch});
    }
    // root events: the tasks started at load (their chains end at a root, in start order)
    void push_root(int32_t node, int64_t delay, Ev ev, uint32_t root)
    {
        int32_t ch = -1;
        if (chains) {
            Chain c, r;
            chain_root(&r, root);
            cpush(cx, &c, ccode(delay, 0), r);
            ch = chain_new(c);
        }
        q.push(QEv{now_ + delay, uid++, node, ev, ch});
    }
    // before running x: its chain must come after the previous event of its node at the same time
    void check_order(const QEv &x)
    {
        if (!chains || x.ch < 0)
            return;
        Last &l = last[x.node + 2];
        if (l.ts == x.ts && l.ch >= 0) {
            ++checked;
            bool inexact;
            int c = ccmp(cx, chainPool[l.ch], chainPool[x.ch], &inexact);
            if (c >= 0) {
                if (++disagree <= 10)
                    fprintf(stderr, "chain order disagrees at %lld node %d: kinds %d then %d (ccmp %d)\n",
                            (long long)x.ts, x.node, (int)lastKind[x.node + 2], (int)x.ev.kind, c);
            }
        }
        if (l.ch >= 0)
            chainFree.push_back(l.ch);
        l.ts = x.ts;
        l.ch = x.ch;
        lastKind[x.node + 2] = x.ev.kind;
        cur = chainPool[x.ch];
        ord = 0;
    }
    std::vector<uint8_t> lastKind;
    void sched(Node &n, int64_t delay, Ev ev) { push(n.id, delay, ev); }
    void tg(Node &, int64_t delay, Ev ev) { push(-2, delay, ev); }
    bool in_root_batch = false;
    uint32_t nextRoot = 1;
    void at_node_twin(int32_t, Ev) {}
    void at_node(int32_t node, int64_t delay, Ev ev)
    {
        if (in_root_batch)
            push_root(node, delay, ev, nextRoot++);
        else
            push(node, delay, ev);
    }
    void link(Node &n, int32_t p, const Pkt &pk, int64_t delay)
    {
        int32_t f;
        if (!freeFrames.empty()) {
            f = freeFrames.back();
            freeFrames.pop_back();
            frames[f] = pk;
        } else {
            f = (int32_t)frames.size();
            frames.push_back(pk);
        }
        Port &pt = port_of(w, n, p);
        push(pt.peerNode, delay, Ev{E_RECV, pt.peerPort, f, 0, 0});
    }
    int32_t route_candidates(Node &n, uint32_t dip, int32_t inPort, int32_t *cand)
    {
        return ub_route(w, n.id, dip, inPort, cand);
    }
    void trace_tx(Node &n, int32_t p, const Pkt &pk)
    {
        if (fport)
            fprintf(fport, "%lld %d %d T %u %d %d %u %d\n", (long long)now_, n.id, p, pk.size, pk.type,
                    pk.task >= 0 && pk.type == P_DATA ? w.task[pk.task].id : -1, pk.psn, pk.srcTpn);
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
    // lifecycle control records are applied synchronously in a single-threaded run
    Model<SeqEngine> *M = nullptr;
    void lc_peer(int32_t node, int32_t ci, int op)
    {
        if (op == 0)
            M->lc_freeze2(ci);
        else
            M->lc_destroy1(ci);
    }
    void lc_destroy2(int32_t, int32_t ci) { M->lc_destroy2(ci); }
    void log_select(Node &, int32_t, int32_t, uint32_t, uint32_t, uint32_t) {}
    void trace_tp_destroy(Node &n, const Tp &t)
    {
        if (ftp)
            fprintf(ftp, "%lld %d %d\n", (long long)now_, n.id, t.tpn);
    }
};

static const char *errname(int32_t e)
{
    static const char *names[] = {"ok", "packet pool exhausted (use --pool)", "segment pool exhausted (use --pool)",
                                  "entry pool exhausted (use --pool)", "remote records exhausted",
                                  "ingress buffer full (a drop)", "no route", "no TP endpoint",
                                  "TP had no packet at allocation commit", "two pending allocations",
                                  "repeated packet", "TA SSN out of window", "TP endpoint revived",
                                  "allocator TP list full", "packet on an unsupported VL"};
    return e >= 0 && e < (int32_t)(sizeof names / sizeof *names) ? names[e] : "?";
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: twins-ubseq CASE_DIR [--out=PREFIX] [--dep-delay=TIME] [--pool=SCALE]\n");
        return 1;
    }
    std::string dir = argv[1], out;
    int64_t depDelay = 100000;
    double pool = 1.0;
    bool chainCheck = false;
    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        if (a.rfind("--out=", 0) == 0)
            out = a.substr(6);
        else if (a.rfind("--dep-delay=", 0) == 0)
            depDelay = parse_time_ps(a.substr(12));
        else if (a.rfind("--pool=", 0) == 0)
            pool = atof(a.c_str() + 7);
        else if (a == "--chains")
            chainCheck = true;
        else {
            fprintf(stderr, "twins-ubseq: unknown option %s\n", a.c_str());
            return 1;
        }
    }
    Case C;
    HostWorld H;
    try {
        load_case(dir, C);
    } catch (std::exception &e) {
        fprintf(stderr, "twins-ubseq: %s\n", e.what());
        return 2;
    }
    std::string err = build_world(C, H, depDelay, pool);
    if (!err.empty()) {
        fprintf(stderr, "twins-ubseq: %s\n", err.c_str());
        return 2;
    }
    SeqEngine E(H);
    World &w = H.w;
    if (!out.empty()) {
        E.fport = fopen((out + ".port").c_str(), "w");
        E.ftask = fopen((out + ".task").c_str(), "w");
        E.ftp = fopen((out + ".tpdel").c_str(), "w");
    }
    if (chainCheck) {
        E.init_chains(w.ntask);
        E.lastKind.assign(w.nnode + 2, 0);
    }
    Model<SeqEngine> M(E, w);
    E.M = &M;
    TrafficGen<SeqEngine> G(E, w);
    // CheckExampleProcess, scheduled at load (before the tasks), every 100 us until all complete
    E.push(-1, 100000000, Ev{E_CHECK, 0, 0, 0, 0});
    E.in_root_batch = true;
    G.schedule_ready();
    E.in_root_batch = false;
    bool stopped = false;
    while (!E.q.empty() && !stopped && !w.mut->err) {
        QEv x = E.q.top();
        E.q.pop();
        E.now_ = x.ts;
        ++E.events;
        if (x.node == -1) {
            if (x.ev.kind == E_STOP)
                stopped = true;
            else if (w.mut->completed < w.ntask)
                E.push(-1, 100000000, Ev{E_CHECK, 0, 0, 0, 0});
            else
                E.push(-1, 0, Ev{E_STOP, 0, 0, 0, 0});
            continue;
        }
        E.check_order(x);
        if (x.node == -2) {
            G.apply(x.ev.a);
            continue;
        }
        Node &n = w.node[x.node];
        if (x.ev.kind == E_RECV) {
            Pkt pk = E.frames[x.ev.b];
            E.freeFrames.push_back(x.ev.b);
            M.receive(n, x.ev.a, pk);
        } else
            M.dispatch(n, x.ev);
    }
    if (E.fport) fclose(E.fport);
    if (E.ftask) fclose(E.ftask);
    if (E.ftp) fclose(E.ftp);
    if (w.mut->err) {
        fprintf(stderr, "twins-ubseq: stopped at %lld ps: %s\n", (long long)E.now_, errname(w.mut->err));
        return 2;
    }
    fprintf(stderr, "twins-ubseq: %d/%d tasks, %llu events, end %lld ps, drops %llu\n", w.mut->completed, w.ntask,
            (unsigned long long)E.events, (long long)E.now_, (unsigned long long)w.mut->drops);
    if (chainCheck)
        fprintf(stderr, "twins-ubseq: chains: %llu same-time pairs checked, %llu disagree, %llu undecided, %llu "
                        "inexact, %llu tail nodes\n",
                (unsigned long long)E.checked, (unsigned long long)E.disagree, E.stat[0], E.stat[1],
                (unsigned long long)E.tailCnt);
    return w.mut->completed == w.ntask ? 0 : 3;
}
