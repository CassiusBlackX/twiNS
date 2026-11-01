// The closed loop: astra-sim's system layer here on the host, the network on the GPU, so that a run
// needs no recording. A completion at t only affects the network send_lat later (SendFlow's delay),
// so the GPU runs ahead of the host by less than that, and the host answers each batch:
// - the GPU records completions (closed_rec), with their chains;
// - the host runs what is due (completions and its timers, below the slot the GPU reached) in
//   ns-3's order, each action's cause and ordinal noted;
// - closed_apply builds the actions' chains from the causes', on the GPU, and files the flows.
// Order: the GPU sorts the completions of one time by their chains (closed_sort). Timers of one
// time run in the order they were scheduled. A timer against a completion: the earlier scheduled,
// which is the larger lead, first; with equal leads their chains decide (closed_cmp, after the
// actions so far reach the GPU: rare).
// Included in rmain.cu.
#pragma once
#include <deque>
#include <map>
#include <sys/wait.h>
#include <tuple>

#include "astra/frontend.h"

static int send_lat_slots(int64_t send_lat, int64_t D) { return (int)((send_lat - 1) / D); }

struct Closed : astra::Network
{
    static constexpr int TCAP = 1 << 18;     // timers alive at once
    static constexpr int CEV_LOG = 20;       // completions recorded and not done with
    static constexpr int ACT_CAP = 1 << 20;  // actions of a sync
    string topo, wl, dag_out;
    astra::Frontend *fe = nullptr;
    // the flows, from the stub's run: by what the system layer asks for
    std::map<std::tuple<int, int, uint64_t>, std::deque<int>> ids;
    vector<int> gpu_of, fe_of;
    struct Main
    {
        uint32_t ord;
        bool timer;
        int64_t id;
    };
    vector<Main> mains;
    vector<std::pair<int, Chain>> main_timers;
    vector<int> main_flows;
    vector<int> tslot, tfree;
    vector<CAct> acts;
    uint32_t parent = 0, base = 0;
    unsigned long long lead_ties = 0, syncs = 0, sort_runs = 0, sorted = 0;
    double t_cpu = 0, t_gpu = 0, t_sync = 0;
    bool in_main = true, bad = false, stop_d = false, stop_t = false;
    int span = 0;

    int slot_alloc()
    {
        if (tfree.empty())
        {
            fprintf(stderr, "more than %d timers at once\n", TCAP);
            exit(1);
        }
        int t = tfree.back();
        tfree.pop_back();
        return t;
    }
    void flow_added(int64_t id, const astra::Flow &f) override
    {
        auto it = ids.find(std::make_tuple(f.src, f.dst, f.size));
        if (it == ids.end() || it->second.empty())
        {
            fprintf(stderr, "flow %d->%d of %llu bytes: not among those of the stub's run\n", f.src, f.dst, (unsigned long long)f.size);
            exit(1);
        }
        int g = it->second.front();
        it->second.pop_front();
        if ((int)gpu_of.size() <= id)
            gpu_of.resize(id + 1);
        gpu_of[id] = g;
        fe_of[g] = (int)id;
        if (in_main)
            mains.push_back(Main{f.ord, false, id});
        else
            acts.push_back(CAct{(uint32_t)g, parent, base + f.ord, 0, f.t, 0});
    }
    void timer_added(int64_t id, const astra::Timer &t) override
    {
        if ((int)tslot.size() <= id)
            tslot.resize(id + 1);
        int k = tslot[id] = slot_alloc();
        if (in_main)
            mains.push_back(Main{t.ord, true, id});
        else
            acts.push_back(CAct{DACT_TIMER | (uint32_t)k, parent, base + t.ord, 0, fe->now, t.delay});
    }

    // the world: main's actions as traced_dag makes them, the rest fired from here
    void world(World &w, int64_t send_lat)
    {
        const int F = w.fi.size();
        w.traced = true;
        w.acts.assign(2 * F, {});
        w.ndeps.assign(F, 1);
        auto push = [&](Chain *c, int64_t lead, int ord) { cpush(c, ccode(lead, ord), Chain(*c)); };
        std::sort(mains.begin(), mains.end(), [](const Main &a, const Main &b) { return a.ord < b.ord; });
        for (size_t i = 0; i < mains.size(); i++)
        {
            Chain c = {};
            c.root = i + 1;
            push(&c, mains[i].timer ? fe->timers[mains[i].id].delay : 0, 0);
            c.hash = 0;
            for (int k = 0; k < NANC; k++)
                c.anc[k] = 0;
            w.troot.push_back(c);
            if (mains[i].timer)
                main_timers.push_back({tslot[mains[i].id], c});
            else
            {
                Chain x = c;
                push(&x, send_lat, 0);
                int g = gpu_of[mains[i].id];
                w.pre.push_back({g, send_lat, x});
                w.ndeps[g] = 0;
            }
        }
    }

    CAct *dev_act = nullptr;
    CEvH *dev_h = nullptr;
    CSort *dev_cs = nullptr;
    ClCtl *dev_cl = nullptr;
    int64_t *dev_slot = nullptr;
    int64_t cev_mask = 0;
    void setup(Sim &s, int span_slots)
    {
        span = span_slots;
        s.cl_span = span;
        s.cev_mask = (1ull << CEV_LOG) - 1;
        cev_mask = s.cev_mask;
        s.cev_h = dev_h = dev_alloc<CEvH>(1ull << CEV_LOG);
        s.cev_c = dev_alloc<Chain>(1ull << CEV_LOG);
        s.cact = dev_act = dev_alloc<CAct>(ACT_CAP);
        s.cs = dev_cs = dev_alloc<CSort>(ACT_CAP);
        ClCtl c = {};
        c.limit = span;
        c.cap = next_cap();
        s.cl = dev_cl = dev_copy(vector<ClCtl>{c});
    }
    void init_timers(Chain *tchain)
    {
        for (auto &m : main_timers)
            CK(hipMemcpy(tchain + m.first, &m.second, sizeof(Chain), hipMemcpyHostToDevice));
    }

    // what the host has waiting: read from the GPU, not yet due
    struct Pending
    {
        int64_t t;
        uint32_t j;
        CEvH h;
        int rank;
    };
    std::deque<Pending> ready;  // sorted, due
    vector<Pending> pool;       // later than the slot reached
    int64_t D = 0;
    int64_t next_cap() const
    {
        int64_t t = INT64_MAX;
        if (fe && fe->has_timer())
            t = fe->next_timer();
        for (auto &p : pool)
            t = std::min(t, p.t);
        return t == INT64_MAX ? INT64_MAX : (D ? t / D : t / 25);
    }
    unsigned long long oldest(unsigned long long head) const
    {
        unsigned long long f = head;
        for (auto &p : pool)
            f = std::min(f, read_idx(p, head));
        for (auto &p : ready)
            f = std::min(f, read_idx(p, head));
        return f;
    }
    // (j is a place in the ring: its index is the last with that place below head)
    unsigned long long read_idx(const Pending &p, unsigned long long head) const
    {
        const unsigned long long m = cev_mask, h = head - 1;
        return (h & ~m) + p.j - ((unsigned long long)p.j > (h & m) ? m + 1 : 0);
    }

    template <class F>
    void run(Sim &s, hipStream_t st, int64_t D_, F launch)
    {
        D = D_;
        auto now = [] { return std::chrono::steady_clock::now(); };
        auto sec = [](auto a, auto b) { return std::chrono::duration<double>(b - a).count(); };
        unsigned long long seen = 0;
        int64_t S = 0;
        int k = 1;
        bool busy = true;
        // (before the first launch: the bounds the setup could not know)
        ClCtl c0 = {};
        c0.limit = span;
        c0.cap = next_cap();
        CK(hipMemcpy(dev_cl, &c0, sizeof(c0), hipMemcpyHostToDevice));
        for (;;)
        {
            auto a = now();
            // (few launches, up to the limit, while completions keep coming; else many: without any
            // the GPU moves the limit itself)
            launch(busy ? k : 1);
            CK(cudaGetLastError());
            ClCtl ctl;
            CK(hipMemcpyAsync(&ctl, s.cl, sizeof(ctl), hipMemcpyDeviceToHost, st));
            CK(hipMemcpyAsync(&S, s.slot, sizeof(S), hipMemcpyDeviceToHost, st));
            CK(hipStreamSynchronize(st));
            auto b = now();
            t_gpu += sec(a, b);
            syncs++;
            k = ctl.k;
            busy = ctl.head != seen;
            // a run that will start again with more room (see main) stops here
            if (syncs % 1024 == 0 && (stop_d || stop_t))
            {
                unsigned long long e[2];
                CK(hipMemcpyAsync(&e[0], s.ev + 9, sizeof(e[0]), hipMemcpyDeviceToHost, st));
                CK(hipMemcpyAsync(&e[1], s.ev + 14, sizeof(e[1]), hipMemcpyDeviceToHost, st));
                CK(hipStreamSynchronize(st));
                if ((stop_t && e[0]) || (stop_d && e[1]))
                    return;
            }
            if (progress && syncs % 2000 == 0)
                fprintf(stderr, "sync %llu slot %lld k %d completions %llu acts %zu gpu %.1f s cpu %.1f s sync %.1f s\n", syncs, (long long)S, ctl.k, ctl.head, acts.size(), t_gpu, t_cpu, t_sync);
            // the completions recorded, as a contiguous range of the ring
            if (ctl.head - seen > (unsigned long long)cev_mask)
            {
                fprintf(stderr, "completions overflowed their ring\n");
                bad = true;
                return;
            }
            vector<CEvH> hs(ctl.head - seen);
            for (unsigned long long i = seen; i < ctl.head;)
            {
                unsigned long long j = i & cev_mask, n = std::min(ctl.head - i, (unsigned long long)cev_mask + 1 - j);
                CK(hipMemcpyAsync(&hs[i - seen], dev_h + j, n * sizeof(CEvH), hipMemcpyDeviceToHost, st));
                i += n;
            }
            if (!hs.empty())
                CK(hipStreamSynchronize(st));
            for (unsigned long long i = seen; i < ctl.head; i++)
                pool.push_back(Pending{hs[i - seen].t, (uint32_t)(i & cev_mask), hs[i - seen], 0});
            seen = ctl.head;
            // the due ones in order
            const int64_t reach = S * D;
            sort_due(s, st, reach);
            auto d = now();
            t_sync += sec(b, d);
            // the system layer
            acts.clear();
            in_main = false;
            tfree_later.clear();
            while (!fe->finished)
            {
                bool tm = fe->has_timer() && fe->next_timer() < reach;
                bool cp = !ready.empty();
                if (!tm && !cp)
                    break;
                if (tm && cp)
                {
                    const int64_t tt = fe->next_timer(), tc = ready.front().t;
                    if (tt != tc)
                        tm = tt < tc;
                    else
                    {
                        const int64_t lt = fe->timers[fe->next_timer_id()].delay, lc = (int64_t)(ready.front().h.code >> CODE_ORD_BITS) - 1;
                        if (lt == lc)
                        {
                            lead_ties++;
                            apply(st, S + span, seen);
                            acts.clear();
                            closed_cmp<<<1, 1, 0, st>>>(tslot[fe->next_timer_id()], (int)ready.front().j);
                            int cmp;
                            CK(hipMemcpyAsync(&cmp, &s.cl->cmp, sizeof(cmp), hipMemcpyDeviceToHost, st));
                            CK(hipStreamSynchronize(st));
                            tm = cmp <= 0;
                        }
                        else
                            tm = lt > lc;
                    }
                }
                if (tm)
                {
                    const int64_t id = fe->next_timer_id();
                    fe->now = fe->next_timer();
                    parent = DACT_TIMER | (uint32_t)tslot[id];
                    base = 0;
                    fe->run_timer();
                    tfree_later.push_back(tslot[id]);
                }
                else
                {
                    const Pending p = ready.front();
                    ready.pop_front();
                    fe->now = p.t;
                    parent = p.j;
                    base = p.h.base;
                    if (p.h.kind == 0)
                        fe->qp_finish(gfe(p.h.flow));
                    else
                        fe->send_finish(gfe(p.h.flow));
                }
                if ((int)acts.size() > ACT_CAP)
                {
                    fprintf(stderr, "more than %d actions in a sync\n", ACT_CAP);
                    bad = true;
                    return;
                }
            }
            auto e = now();
            t_cpu += sec(d, e);
            if (fe->finished)
                return;
            // nothing more to come
            if (ctl.idle && acts.empty() && pool.empty() && ready.empty() && !fe->has_timer())
                return;
            apply(st, S + span, seen);
            for (int t : tfree_later)
                tfree.push_back(t);
            CK(hipStreamSynchronize(st));
            t_sync += sec(e, now());
        }
    }
    const bool progress = getenv("TWINS_AI_PROGRESS") != nullptr;
    bool single = false;
    void apply(hipStream_t st, int64_t limit, unsigned long long seen)
    {
        if (!acts.empty())
            CK(hipMemcpyAsync(dev_act, acts.data(), acts.size() * sizeof(CAct), hipMemcpyHostToDevice, st));
        for (auto &x : acts)
            single |= !(x.child & DACT_TIMER);
        closed_apply<<<1, 1, 0, st>>>((int)acts.size(), limit, next_cap(), single, seen, oldest(seen));
        single = false;
    }
    vector<int> tfree_later;
    int gfe(int g) const { return fe_of[g]; }

    // pool's completions below reach, in order, onto ready
    void sort_due(Sim &s, hipStream_t st, int64_t reach)
    {
        vector<Pending> due;
        size_t k = 0;
        for (auto &p : pool)
            if (p.t < reach)
                due.push_back(p);
            else
                pool[k++] = p;
        pool.resize(k);
        if (due.empty())
            return;
        std::stable_sort(due.begin(), due.end(), [](const Pending &a, const Pending &b) { return a.t < b.t; });
        vector<CSort> cs;
        for (size_t i = 0; i < due.size();)
        {
            size_t e = i;
            while (e < due.size() && due[e].t == due[i].t)
                e++;
            if (e - i > 1)
            {
                int g0 = cs.size(), g1 = g0 + (e - i);
                for (size_t q = i; q < e; q++)
                    cs.push_back(CSort{due[q].j, g0, g1, 0});
            }
            i = e;
        }
        if (!cs.empty())
        {
            sort_runs++;
            sorted += cs.size();
            CK(hipMemcpyAsync(dev_cs, cs.data(), cs.size() * sizeof(CSort), hipMemcpyHostToDevice, st));
            closed_sort<<<((int)cs.size() + 31) / 32, 32, 0, st>>>((int)cs.size());
            CK(hipMemcpyAsync(cs.data(), dev_cs, cs.size() * sizeof(CSort), hipMemcpyDeviceToHost, st));
            CK(hipStreamSynchronize(st));
            size_t w = 0;
            for (size_t i = 0; i < due.size();)
            {
                size_t e = i;
                while (e < due.size() && due[e].t == due[i].t)
                    e++;
                if (e - i > 1)
                {
                    vector<Pending> o(e - i);
                    for (size_t q = 0; q < e - i; q++)
                        o[cs[w + q].rank] = due[i + q];
                    std::copy(o.begin(), o.end(), due.begin() + i);
                    w += e - i;
                }
                i = e;
            }
        }
        ready.insert(ready.end(), due.begin(), due.end());
    }

    void report()
    {
        printf("closed loop: %zu flows, %zu timers, %llu callbacks, %llu syncs, %llu of them with ties (%llu completions), %llu lead ties; GPU %.3f s, system layer %.3f s, sync %.3f s\n",
               fe->flows.size(), fe->timers.size(), (unsigned long long)fe->callbacks, syncs, sort_runs, sorted, lead_ties, t_gpu, t_cpu, t_sync);
        if (lead_ties)
            fprintf(stderr, "warning: %llu timers tied with completions of their time and lead\n", lead_ties);
        if (!dag_out.empty())
            write_dag();
    }
    // the actions as trace2dag.py writes them, to compare with a recording
    void write_dag()
    {
        FILE *f = fopen(dag_out.c_str(), "w");
        if (!f)
            return;
        for (size_t i = 0; i < fe->flows.size(); i++)
            fprintf(f, "flow %zu %d %d %llu\n", i, fe->flows[i].src, fe->flows[i].dst, (unsigned long long)fe->flows[i].size);
        for (size_t i = 0; i < fe->timers.size(); i++)
            fprintf(f, "timer %zu %lld\n", i, (long long)fe->timers[i].delay);
        auto on = [&](const char *c, size_t i, const astra::Cause &k, uint32_t ord) {
            if (k.kind == 'm')
                fprintf(f, "on %s%zu m\n", c, i); // (ns-3's uids: only their order counts)
            else
                fprintf(f, "on %s%zu %c%lld %u\n", c, i, k.kind, (long long)k.id, ord);
        };
        for (size_t i = 0; i < fe->flows.size(); i++)
            on("f", i, fe->flows[i].cause, fe->flows[i].ord);
        for (size_t i = 0; i < fe->timers.size(); i++)
            on("t", i, fe->timers[i].cause, fe->timers[i].ord);
        fclose(f);
    }
};
static Closed *g_cl = nullptr;

// the flows from a child running the system layer on a stub network, as a DAG's flow lines; then
// the system layer here, up to its first actions
static bool closed_prepare(const string &topo, const string &wl, const string &flow_file, const string &dag_out, bool cache)
{
    char *atopo = realpath(topo.c_str(), nullptr), *awl = realpath(wl.c_str(), nullptr);
    char *aout = realpath(".", nullptr);
    if (!atopo || !awl)
    {
        fprintf(stderr, "cannot find %s or %s\n", topo.c_str(), wl.c_str());
        return false;
    }
    const string path = flow_file[0] == '/' ? flow_file : string(aout) + "/" + flow_file;
    fflush(stdout);
    auto t0 = std::chrono::steady_clock::now();
    const bool have = cache && std::ifstream(path);
    pid_t pid = have ? 1 : fork();
    if (pid == 0)
    {
        // (its stdout, and its SimAI csv files, are not ours)
        if (!freopen("/dev/null", "w", stdout))
            _exit(1);
        vector<astra::Flow> flows;
        auto st = astra::stub_run(atopo, awl, 400, &flows);
        FILE *f = fopen(path.c_str(), "w");
        if (!f || !st.finished)
            _exit(1);
        for (size_t i = 0; i < flows.size(); i++)
            fprintf(f, "flow %zu %d %d %llu\n", i, flows[i].src, flows[i].dst, (unsigned long long)flows[i].size);
        fclose(f);
        _exit(0);
    }
    int stc = 0;
    if (!have)
        waitpid(pid, &stc, 0);
    if (!WIFEXITED(stc) || WEXITSTATUS(stc) != 0)
    {
        fprintf(stderr, "the system layer's run on a stub network failed\n");
        return false;
    }
    Closed *c = g_cl = new Closed;
    c->topo = atopo;
    c->wl = awl;
    c->dag_out = dag_out;
    std::ifstream in(path);
    string w;
    int n = 0;
    while (in >> w)
    {
        unsigned id, a, b;
        unsigned long long sz;
        in >> id >> a >> b >> sz;
        c->ids[std::make_tuple((int)a, (int)b, (uint64_t)sz)].push_back(n++);
    }
    c->fe_of.assign(n, -1);
    for (int t = Closed::TCAP - 1; t >= 0; t--)
        c->tfree.push_back(t);
    c->fe = new astra::Frontend(atopo, awl, c);
    c->fe->fire();
    printf("the system layer's flows: %d (%.3f s%s)\n", n, std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count(), have ? ", kept" : " on a stub network");
    return true;
}
