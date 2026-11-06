// twins-ubseq: model.h under a sequential engine that orders events exactly like single-threaded
// ns-3 (MapScheduler: time, then uid, uid a counter over the Schedule calls). It is the reference
// for the parallel engines: the same handlers, a different event order mechanism.
//
// Usage: twins-ubseq CASE_DIR [--out=PREFIX] [--dep-delay=TIME] [--pool=SCALE]
//   PREFIX.port : time_ps node port T|R size ...   PREFIX.task : time_ps node task S|C
#include "setup.h"
#include <cstdio>
#include <queue>

using namespace ub;

struct QEv {
    int64_t ts;
    uint64_t uid;
    int32_t node;   // -2: the traffic generator, -1: the runner
    Ev ev;
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

    SeqEngine(HostWorld &h) : H(h), w(h.w) {}
    int64_t now() const { return now_; }
    void push(int32_t node, int64_t delay, Ev ev) { q.push(QEv{now_ + delay, uid++, node, ev}); }
    void sched(Node &n, int64_t delay, Ev ev) { push(n.id, delay, ev); }
    void tg(Node &, int64_t delay, Ev ev) { push(-2, delay, ev); }
    void at_node(int32_t node, int64_t delay, Ev ev) { push(node, delay, ev); }
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
        int32_t dn = ip_node(dip);
        uint32_t last = dip & 0xff;
        int32_t dp = last ? (int32_t)last - 1 : 0;
        size_t key = ((size_t)n.id * w.nnode + dn) * HostWorld::MAXHP + dp;
        int32_t c = 0;
        for (int32_t i = 0; i < H.rtCnt[key]; ++i) {
            int32_t p = H.rtPorts[H.rtOff[key] + i];
            if (p != inPort)
                cand[c++] = p;
        }
        return c;
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
    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        if (a.rfind("--out=", 0) == 0)
            out = a.substr(6);
        else if (a.rfind("--dep-delay=", 0) == 0)
            depDelay = parse_time_ps(a.substr(12));
        else if (a.rfind("--pool=", 0) == 0)
            pool = atof(a.c_str() + 7);
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
    Model<SeqEngine> M(E, w);
    TrafficGen<SeqEngine> G(E, w);
    // CheckExampleProcess, scheduled at load (before the tasks), every 100 us until all complete
    E.push(-1, 100000000, Ev{E_CHECK, 0, 0, 0, 0});
    G.schedule_ready();
    bool stopped = false;
    while (!E.q.empty() && !stopped && !w.err) {
        QEv x = E.q.top();
        E.q.pop();
        E.now_ = x.ts;
        ++E.events;
        if (x.node == -1) {
            if (x.ev.kind == E_STOP)
                stopped = true;
            else if (w.completed < w.ntask)
                E.push(-1, 100000000, Ev{E_CHECK, 0, 0, 0, 0});
            else
                E.push(-1, 0, Ev{E_STOP, 0, 0, 0, 0});
            continue;
        }
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
    if (w.err) {
        fprintf(stderr, "twins-ubseq: stopped at %lld ps: %s\n", (long long)E.now_, errname(w.err));
        return 2;
    }
    fprintf(stderr, "twins-ubseq: %d/%d tasks, %llu events, end %lld ps, drops %llu\n", w.completed, w.ntask,
            (unsigned long long)E.events, (long long)E.now_, (unsigned long long)w.drops);
    return w.completed == w.ntask ? 0 : 3;
}
