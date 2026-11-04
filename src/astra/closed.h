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
#include <unordered_map>

#include "astra/frontend.h"

static int send_lat_slots(int64_t send_lat, int64_t D) { return (int)((send_lat - 1) / D); }
// --nvl-record=F writes every completion of a flow between GPUs of one server (FlowInfo::fast) with
// its chain; --nvl-oracle=F does not run those flows, and puts F's completions in the ring when the
// GPU reaches their times, as if it had made them. Same completions, same chains: the system layer
// must do exactly what it did. What is left on the GPU is the network between the servers.
static string g_nvl_record, g_nvl_oracle;
static int g_nvl_islands = 0;
static int g_nvl_ringlog = 24; // --nvl-ringlog: an island's tail ring holds 2^this nodes
static unsigned long long g_isl_ev[40]; // the islands' counters, summed (added to the GPU's in the report)
struct NvlPre
{
    int g;
    int64_t ts;
    Chain c;
};
static vector<NvlPre> g_nvl_pre; // main's flows within servers, ready at the start
static uint32_t *g_nvl_cnt_host = nullptr; // (the GPU's counts of events inside servers: rstep.cuh)
static int g_nvl_nsw = 0;
// The file: items in the order they were recorded. A tail node (tag 0) comes before the chains that
// reach it, and after the older node it points to; a completion (tag 1) can be put in once the
// GPU has passed avail (the slot the recording run had reached when it read it), which is before
// its time.
#include "rdma/isl_shm.h"
#include <unordered_set>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <csignal>
#include <ftw.h>

// --nvl-islands: the flows between GPUs of one server run on CPU cores, a twins-island process per
// server (src/rdma/island.h), alongside the GPU. Each sync the islands get the messages starting
// their flows and run to the GPU's limit; their completions go into the GPU's ring of completions,
// as the GPU's own. Chains cross both ways with the tail nodes they reach: the islands' nodes the GPU
// reads where they are, the GPU's go into one table all islands read (IslCopies).
constexpr int NVL_ISL_DEPTH = 1 << 30; // the GPU's tail nodes of a chain put in the islands' table: all it reaches (each once)
struct NvlIslands
{
    struct Node
    {
        uint32_t code, cnt;
        uint64_t next;
    };
    int n = 0;
    vector<int> of_sw;               // a switch's island, or -1
    vector<IslShm *> shm;
    vector<pid_t> pid;
    vector<string> names;
    vector<vector<NvlRec>> queue;    // items for an island's next command
    IslCopies *copies = nullptr;       // the GPU's nodes the islands have (isl_shm.h)
    uint32_t copy_gen = 0;
    string copy_prefix;
    std::unordered_map<uint64_t, Node> gnodes; // the GPU's nodes the walks found
    vector<void *> rings;            // the islands' tail rings (shared memory, mapped for the GPU)
    RemoteRings rr_host = {}, *drr = nullptr; // (their lo: see tail_at)
    vector<uint64_t> nodes_at_go;    // each island's tail nodes when its run began
    unsigned long long wide_windows = 0; // runs in which an island wrote half a ring or more
    int RING_LOG = 24;              // (--nvl-ringlog)
    long long live = 0;              // flows given to islands and not complete
    int64_t slot = 0;                // where the islands are
    int64_t sent_limit = 0;          // the limit of the last run
    string dir;
    double t_wait = 0, t_xfer = 0;
    unsigned long long xn_seen = 0, items_in = 0, items_out = 0;

    // the files, segments and processes: servers spread over P processes (--nvl-islands=P), each
    // running its servers as a batch of worlds
    vector<vector<int>> wsw;         // a process's servers, in its job order
    vector<vector<vector<int>>> wflows; // and their flows (global ids)
    void start(Sim &s, const vector<FlowInfo> &fi, const vector<int> &h_srv, const string &topo, int64_t D, int P)
    {
        const int S = s.S;
        RING_LOG = g_nvl_ringlog;
        of_sw.assign(S, -1);
        vector<vector<int>> flows(S);
        for (size_t g = 0; g < fi.size(); g++)
            if (fi[g].fast)
                flows[h_srv[fi[g].src]].push_back((int)g);
        vector<int> xs;
        for (int x = 0; x < S; x++)
            if (!flows[x].empty())
                xs.push_back(x);
        n = std::max(1, std::min(P, (int)xs.size()));
        if (xs.empty())
        {
            n = 0;
            return;
        }
        wsw.assign(n, {});
        wflows.assign(n, {});
        for (size_t i = 0; i < xs.size(); i++)
        {
            wsw[i % n].push_back(xs[i]);
            of_sw[xs[i]] = (int)(i % n);
        }
        // the topology's lines: header, switch ids, links (SimAI's format)
        std::ifstream tf(topo);
        string head, ids, line;
        std::getline(tf, head);
        std::getline(tf, ids);
        vector<string> links;
        while (std::getline(tf, line))
            links.push_back(line);
        const int H = s.H;
        char tmpl[] = "/dev/shm/twins-isl-XXXXXX";
        if (!mkdtemp(tmpl))
        {
            fprintf(stderr, "nvl: cannot make a directory in /dev/shm\n");
            exit(1);
        }
        dir = tmpl;
        static vector<string> *to_unlink = &names;
        static string *to_remove = &dir;
        static vector<pid_t> *to_kill = &pid;
        static pid_t owner = getpid(); // (a forked child of twins-ai has the handler too: it leaves this to its parent)
        atexit([] {
            for (auto &x : *to_unlink)
                shm_unlink(x.c_str());
        });
        // (killed, as by timeout: the segments and the directory would stay in /dev/shm, in memory)
        for (int sig : {SIGTERM, SIGINT, SIGHUP})
            signal(sig, [](int sig) {
                if (getpid() != owner)
                    _exit(128 + sig);
                for (pid_t p : *to_kill) // (the islands first: they write in the directory)
                {
                    kill(p, SIGKILL);
                    waitpid(p, nullptr, 0);
                }
                for (auto &x : *to_unlink)
                    shm_unlink(x.c_str());
                if (!to_remove->empty())
                    nftw(to_remove->c_str(), [](const char *f, const struct stat *, int, struct FTW *) { remove(f); return 0; }, 16, FTW_DEPTH | FTW_PHYS);
                _exit(128 + sig);
            });
        const char *bin = getenv("TWINS_ISLAND");
        string island_bin = bin ? bin : "";
        if (island_bin.empty())
        {
            char self[4096];
            ssize_t k = readlink("/proc/self/exe", self, sizeof(self) - 1);
            self[k > 0 ? k : 0] = 0;
            string d = self;
            island_bin = d.substr(0, d.rfind('/')) + "/twins-island";
        }
        // the islands' tail rings, readable by the GPU (a pos with top byte k + 1 is island k's)
        const string ring_prefix = "/twins-isl-" + std::to_string(getpid()) + "-ring";
        RemoteRings rr = {};
        if (n > 64)
        {
            fprintf(stderr, "nvl: at most 64 islands\n");
            exit(1);
        }
        for (int k = 0; k < n; k++)
        {
            const string name = ring_prefix + std::to_string(k);
            const size_t bytes = sizeof(TailNode) << RING_LOG;
            int fd = shm_open(name.c_str(), O_CREAT | O_RDWR | O_TRUNC, 0600);
            void *p = fd >= 0 && !ftruncate(fd, bytes) ? mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0) : MAP_FAILED;
            if (fd >= 0)
                close(fd);
            if (p == MAP_FAILED)
            {
                fprintf(stderr, "nvl: cannot make %s\n", name.c_str());
                exit(1);
            }
            CK(hipHostRegister(p, bytes, hipHostRegisterMapped));
            void *dp;
            CK(hipHostGetDevicePointer(&dp, p, 0));
            rr.n[k] = static_cast<const TailNode *>(dp);
            rr.mask[k] = (1ull << RING_LOG) - 1;
            rings.push_back(p);
            names.push_back(name);
        }
        rr_host = rr;
        drr = const_cast<RemoteRings *>(dev_copy(vector<RemoteRings>{rr}));
        const RemoteRings *cdrr = drr;
        CK(hipMemcpyToSymbol(HIP_SYMBOL(g_remote), &cdrr, sizeof(cdrr)));
        nodes_at_go.assign(n, 0);
        for (int k = 0; k < n; k++)
        {
            string pd = dir + "/p" + std::to_string(k);
            mkdir(pd.c_str(), 0700);
            FILE *jf = fopen((pd + ".jobs").c_str(), "w");
            FILE *mf = fopen((pd + ".map").c_str(), "w");
            for (int x : wsw[k])
            {
                // its GPUs, by host id; its links to the NVSwitch (node H + x)
                vector<int> gpus;
                for (int h = 0; h < H; h++)
                    if (h_srv[h] == x)
                        gpus.push_back(h);
                std::map<int, int> lid;
                for (size_t i = 0; i < gpus.size(); i++)
                    lid[gpus[i]] = (int)i;
                const int ng = gpus.size();
                string base = pd + "/s" + std::to_string(x);
                FILE *f = fopen((base + ".topo").c_str(), "w");
                fprintf(f, "%d %d 1 0 %d H100\n%d\n", ng + 1, ng, ng, ng);
                for (auto &l : links)
                {
                    std::istringstream ls(l);
                    int a, b;
                    if (!(ls >> a >> b))
                        continue;
                    string rest;
                    std::getline(ls, rest);
                    if (b == H + x && lid.count(a))
                        fprintf(f, "%d %d%s\n", lid[a], ng, rest.c_str());
                    else if (a == H + x && lid.count(b))
                        fprintf(f, "%d %d%s\n", lid[b], ng, rest.c_str());
                }
                fclose(f);
                f = fopen((base + ".dag").c_str(), "w");
                for (size_t i = 0; i < flows[x].size(); i++)
                {
                    const FlowInfo &y = fi[flows[x][i]];
                    fprintf(f, "flow %zu %d %d %llu\n", i, lid[y.src], lid[y.dst], (unsigned long long)y.size);
                }
                fclose(f);
                for (int g : flows[x])
                    fprintf(mf, "%d\n", g);
                wflows[k].push_back(flows[x]);
                fprintf(jf, "s%d %s.topo %s.dag\n", x, base.c_str(), base.c_str());
            }
            fclose(jf);
            fclose(mf);
            string name = "/twins-isl-" + std::to_string(getpid()) + "-" + std::to_string(k);
            IslShm *m = isl_map(name.c_str(), true, 1 << 18, 1 << 18);
            if (!m)
            {
                fprintf(stderr, "nvl: cannot make %s\n", name.c_str());
                exit(1);
            }
            shm.push_back(m);
            names.push_back(name);
            // the island: the config and every KEY=VALUE given here, the cluster's RTT and window
            vector<string> args = {island_bin, g_argv[1], "--batch=" + pd + ".jobs", "--outdir=" + pd, "--isl-map=" + pd + ".map",
                                   "--isl-shm=" + name, "--tail=16", "--isl-rings=" + ring_prefix, "--isl-nrings=" + std::to_string(n),
                                   "--isl-ringlog=" + std::to_string(RING_LOG), "--isl-side=" + std::to_string(k + 1),
                                   "NVL_BASE_RTT=" + std::to_string(s.P.base_rtt),
                                   "NVL_WIN=" + std::to_string(s.P.win)};
            for (int i = 2; i < g_argc; i++)
                if (g_argv[i][0] != '-' && strchr(g_argv[i], '='))
                    args.push_back(g_argv[i]);
            pid_t p = fork();
            if (p == 0)
            {
                prctl(PR_SET_PDEATHSIG, SIGKILL); // (it ends with twins-ai, whatever happens)
                FILE *lg = freopen((pd + ".log").c_str(), "w", stdout);
                (void)lg;
                dup2(fileno(stdout), 2);
                vector<char *> av;
                for (auto &a : args)
                    av.push_back(const_cast<char *>(a.c_str()));
                av.push_back(nullptr);
                execv(av[0], av.data());
                fprintf(stderr, "cannot run %s\n", av[0]);
                _exit(127);
            }
            pid.push_back(p);
        }
        queue.assign(n, {});
        copy_prefix = "/twins-isl-" + std::to_string(getpid()) + "-copies";
        copies_make(1ull << 20);
        printf("nvl: %zu servers on %d islands (%s)\n", xs.size(), n, dir.c_str());
    }
    int island_of_flow(const FlowInfo &y, const vector<int> &h_srv) const { return of_sw[h_srv[y.src]]; }
    // a command to an island (its queue first, in pieces if needed); waits only for the pieces
    // (returns done_seq as it was before the last part: an answer to the command moves it on; a queue
    // longer than in_cap goes as ISL_LOADs first, each answered, so the value before them would not do)
    uint32_t command(int k, uint32_t cmd, int64_t limit)
    {
        uint32_t d_last = 0;
        IslShm *m = shm[k];
        auto &q = queue[k];
        size_t at = 0;
        do
        {
            const uint64_t c = std::min<uint64_t>(q.size() - at, m->in_cap);
            memcpy((void *)m->in(), q.data() + at, c * sizeof(NvlRec));
            m->in_n = c;
            at += c;
            items_in += c;
            const bool last = at == q.size();
            m->cmd = last ? cmd : ISL_LOAD;
            m->limit = limit;
            const uint32_t d = m->done_seq.load(std::memory_order_acquire);
            d_last = d;
            isl_wake(m->cmd_seq);
            if (!last)
                isl_wait(m->done_seq, d);
            else
                break;
        } while (true);
        q.clear();
        return d_last;
    }
    vector<uint32_t> done_at;
    void go(int64_t limit, hipStream_t st)
    {
        done_at.assign(n, 0);
        // (the GPU takes no island node that this run could overwrite: see tail_at)
        const uint64_t half = 1ull << (RING_LOG - 1);
        for (int k = 0; k < n; k++)
        {
            nodes_at_go[k] = shm[k]->nodes;
            rr_host.lo[k] = nodes_at_go[k] > half ? nodes_at_go[k] - half : 0;
        }
        CK(hipMemcpyAsync(drr->lo, rr_host.lo, n * sizeof(uint64_t), hipMemcpyHostToDevice, st));
        for (int k = 0; k < n; k++)
        {
            done_at[k] = command(k, ISL_RUN, limit);
        }
        sent_limit = limit;
    }
    // each island's answer: what it made, in order
    void collect(vector<vector<NvlRec>> &out)
    {
        auto t0 = std::chrono::steady_clock::now();
        out.assign(n, {});
        for (int k = 0; k < n; k++)
        {
            IslShm *m = shm[k];
            isl_wait(m->done_seq, done_at[k]);
            for (;;)
            {
                out[k].insert(out[k].end(), m->out(), m->out() + m->out_n);
                items_out += m->out_n;
                if (!m->more)
                    break;
                const uint32_t d = m->done_seq.load(std::memory_order_acquire);
                m->cmd = ISL_MORE;
                isl_wake(m->cmd_seq);
                isl_wait(m->done_seq, d);
            }
            slot = m->slot;
            if (m->nodes - nodes_at_go[k] >= (1ull << (RING_LOG - 1)))
                wide_windows++; // (the GPU may have read a node as it was overwritten: a lost tail)
        }
        t_wait += std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    }
    // the islands' items into the GPU's rings: nodes renumbered, completions after the GPU's own
    template <typename P>
    unsigned long long inject(Sim &s, hipStream_t st, vector<vector<NvlRec>> &out, CEvH *dev_h, unsigned long long head,
                              unsigned long long mask, P &&pending)
    {
        auto t0 = std::chrono::steady_clock::now();
        vector<CEvH> hs;
        vector<Chain> cs;
        for (int k = 0; k < n; k++)
            for (auto &r : out[k])
                if (r.tag == 1)
                {
                    hs.push_back(r.h);
                    cs.push_back(r.c); // (its tail: the island's nodes, which the GPU reads where they are)
                    if (r.h.kind == 0)
                        live--;
                }
        for (size_t q = 0; q < hs.size();)
        {
            const unsigned long long j = (head + q) & mask, c = std::min<unsigned long long>(hs.size() - q, mask + 1 - j);
            CK(hipMemcpyAsync(dev_h + j, &hs[q], c * sizeof(CEvH), hipMemcpyHostToDevice, st));
            CK(hipMemcpyAsync(s.cev_c + j, &cs[q], c * sizeof(Chain), hipMemcpyHostToDevice, st));
            q += c;
        }
        for (size_t i = 0; i < hs.size(); i++, head++)
            pending(hs[i], (uint32_t)(head & mask));
        CK(hipMemcpyAsync(&s.cl->head, &head, sizeof(head), hipMemcpyHostToDevice, st));
        CK(hipStreamSynchronize(st));
        t_xfer += std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        return head;
    }
    // the messages the GPU made for islands' flows (closed_apply, dag_msg), with the GPU's tail nodes
    // their chains reach that the island does not have, onto its queue
    void take_starts(Sim &s, hipStream_t st, const vector<FlowInfo> &fi, const vector<int> &h_srv, int64_t made)
    {
        unsigned long long xn;
        CK(hipMemcpy(&xn, s.nvl_xn, sizeof(xn), hipMemcpyDeviceToHost));
        if (xn - xn_seen > s.nvl_xcap)
        {
            fprintf(stderr, "nvl: more than %llu messages for islands in a sync\n", s.nvl_xcap);
            exit(1);
        }
        if (xn == xn_seen)
            return;
        auto t0 = std::chrono::steady_clock::now();
        const int m = (int)(xn - xn_seen), cap = 1 << 23;
        static uint32_t *d_idx = nullptr;
        static int d_n = 0;
        static unsigned int *d_k = nullptr;
        static TailNode *d_out = nullptr;
        static unsigned long long *d_mark = nullptr;
        if (!d_out)
        {
            TailPool tp;
            CK(hipMemcpyFromSymbol(&tp, HIP_SYMBOL(g_tail), sizeof(tp)));
            d_mark = dev_alloc<unsigned long long>(tp.mask + 1);
            d_out = dev_alloc<TailNode>(cap);
            d_k = dev_alloc<unsigned int>(1);
        }
        if (m > d_n)
        {
            if (d_idx)
                CK(hipFree(d_idx));
            d_n = std::max(m, 2 * d_n);
            d_idx = dev_alloc<uint32_t>(d_n);
        }
        vector<uint32_t> idx(m);
        for (int e = 0; e < m; e++)
            idx[e] = (uint32_t)((xn_seen + e) & (s.nvl_xcap - 1));
        CK(hipMemcpyAsync(d_idx, idx.data(), m * sizeof(uint32_t), hipMemcpyHostToDevice, st));
        CK(hipMemsetAsync(d_k, 0, sizeof(unsigned), st));
        nvl_walk_par<<<(m + 63) / 64, 64, 0, st>>>(s.nvl_xc, d_idx, m, d_out, cap, d_k, d_mark, NVL_ISL_DEPTH);
        unsigned kk;
        CK(hipMemcpyAsync(&kk, d_k, sizeof(kk), hipMemcpyDeviceToHost, st));
        CK(hipStreamSynchronize(st));
        if (kk > (unsigned)cap)
        {
            fprintf(stderr, "nvl: more than %d new tail nodes in a sync\n", cap);
            exit(1);
        }
        vector<TailNode> nodes(kk);
        if (kk)
            CK(hipMemcpy(nodes.data(), d_out, kk * sizeof(TailNode), hipMemcpyDeviceToHost));
        for (auto &t : nodes)
            gnodes[t.pos] = Node{t.code, t.cnt, t.next};
        vector<Chain> cs(m);
        vector<CEvH> hs(m);
        vector<int64_t> as(m);
        for (int q = 0; q < m;)
        {
            const unsigned long long j = (xn_seen + q) & (s.nvl_xcap - 1), c = std::min<unsigned long long>(m - q, s.nvl_xcap - j);
            CK(hipMemcpy(cs.data() + q, s.nvl_xc + j, c * sizeof(Chain), hipMemcpyDeviceToHost));
            CK(hipMemcpy(hs.data() + q, s.nvl_xh + j, c * sizeof(CEvH), hipMemcpyDeviceToHost));
            CK(hipMemcpy(as.data() + q, s.nvl_xa + j, c * sizeof(int64_t), hipMemcpyDeviceToHost));
            q += c;
        }
        for (int e = 0; e < m; e++)
        {
            const int k = island_of_flow(fi[hs[e].flow], h_srv);
            send_tail(cs[e].tail);
            NvlRec r = {};
            r.tag = 2;
            r.avail = as[e];
            r.h = hs[e];
            r.c = cs[e];
            r.n.pos = (uint64_t)made;
            queue[k].push_back(r);
        }
        gnodes.clear(); // (all in the table now)
        xn_seen = xn;
        t_xfer += std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    }
    // a new table of the GPU's nodes for the islands, with what the old one held
    void copies_make(uint64_t cap)
    {
        const string name = copy_prefix + std::to_string(++copy_gen);
        const size_t bytes = IslCopies::bytes(cap);
        int fd = shm_open(name.c_str(), O_CREAT | O_RDWR | O_TRUNC, 0600);
        void *p = fd >= 0 && !ftruncate(fd, bytes) ? mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0) : MAP_FAILED;
        if (fd >= 0)
            close(fd);
        if (p == MAP_FAILED || name.size() >= sizeof(shm[0]->copy_name))
        {
            fprintf(stderr, "nvl: cannot make %s\n", name.c_str());
            exit(1);
        }
        names.push_back(name);
        IslCopies *c = static_cast<IslCopies *>(p);
        c->mask = cap - 1;
        c->n = 0;
        if (copies)
        {
            const TailNode *o = copies->slots();
            for (uint64_t i = 0; i <= copies->mask; i++)
                if (o[i].pos)
                    c->put(o[i]);
            munmap(copies, IslCopies::bytes(copies->mask + 1));
            shm_unlink((copy_prefix + std::to_string(copy_gen - 1)).c_str()); // (the islands keep it mapped until they move)
        }
        copies = c;
        for (int k = 0; k < n; k++)
        {
            shm[k]->copy_gen = copy_gen;
            strcpy(shm[k]->copy_name, name.c_str());
        }
    }
    // the GPU's nodes of a tail not yet in the islands' table, into it (the islands wait for a command)
    void send_tail(uint64_t p)
    {
        while (p && !(p >> TAIL_SIDE) && !copies->find(p)) // (an island's node: the islands read it where it is)
        {
            auto it = gnodes.find(p);
            if (it == gnodes.end())
                break; // (overwritten in the GPU's ring before it was found: lost, if reached)
            if (2 * (copies->n + 1) > copies->mask + 1)
                copies_make(2 * (copies->mask + 1));
            copies->put(TailNode{it->second.code, it->second.cnt, it->second.next, p});
            p = it->second.next;
        }
    }
    // main's flows within servers, ready at the start (their chains are main's: no tails)
    void pre(const vector<NvlPre> &ps, const vector<FlowInfo> &fi, const vector<int> &h_srv)
    {
        for (auto &p : ps)
        {
            NvlRec r = {};
            r.tag = 3;
            r.h.t = p.ts;
            r.h.flow = p.g;
            r.c = p.c;
            queue[island_of_flow(fi[p.g], h_srv)].push_back(r);
            live++;
        }
        for (int k = 0; k < n; k++)
        {
            done_at.assign(n, 0);
            isl_wait(shm[k]->done_seq, command(k, ISL_LOAD, 0));
        }
    }
    // the end: the islands write their flows' completion times; these, by global id
    void finish(vector<int64_t> &start, vector<int64_t> &fct, unsigned &done)
    {
        double busy = 0;
        uint64_t nodes_max = 0;
        for (int k = 0; k < n; k++)
        {
            const uint32_t d = shm[k]->done_seq.load(std::memory_order_acquire);
            busy += shm[k]->busy_s;
            nodes_max = std::max<uint64_t>(nodes_max, shm[k]->nodes);
            for (int i = 0; i < 40; i++)
                g_isl_ev[i] += shm[k]->ev[i];
            g_isl_ev[9] += k == 0 ? wide_windows : 0;
            if (shm[k]->ev[8] || shm[k]->ev[9])
                fprintf(stderr, "nvl: island %d: undecided ties %llu, on lost tails %llu\n", k, shm[k]->ev[8], shm[k]->ev[9]);
            shm[k]->cmd = ISL_QUIT;
            isl_wake(shm[k]->cmd_seq);
            isl_wait(shm[k]->done_seq, d);
            int st;
            waitpid(pid[k], &st, 0);
            for (size_t w = 0; w < wsw[k].size(); w++)
            {
                FILE *f = fopen((dir + "/p" + std::to_string(k) + "/s" + std::to_string(wsw[k][w]) + ".fct").c_str(), "r");
                long long i, a, b;
                const vector<int> &mp = wflows[k][w];
                while (f && fscanf(f, "%lld %lld %lld", &i, &a, &b) == 3)
                    if (i >= 0 && i < (long long)mp.size() && fct[mp[i]] < 0)
                    {
                        start[mp[i]] = a;
                        fct[mp[i]] = b;
                        done++;
                    }
                if (f)
                    fclose(f);
            }
        }
        for (auto &x : names)
            shm_unlink(x.c_str());
        if (!dir.empty())
        {
            string cmd = "rm -rf '" + dir + "'";
            if (system(cmd.c_str()))
                fprintf(stderr, "nvl: could not remove %s\n", dir.c_str());
        }
        printf("nvl: islands busy %.3f s in all; twins-ai waited %.3f s for them, %.3f s moving items; %llu items in, %llu out; up to %llu tail nodes an island (ring %llu)\n", busy,
               t_wait, t_xfer, items_in, items_out, (unsigned long long)nodes_max, 1ull << RING_LOG);
        // (a ring that wrapped is fine while no comparison reaches back past it: one that does is
        // counted as on a lost tail, and the run is redone with larger rings)
    }
};

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
    vector<char> fast;
    vector<NvlRec> orecs;
    std::unordered_map<uint64_t, uint64_t> opos; // a recorded tail node's pos: its pos in this run
    size_t onext = 0;
    FILE *rec_f = nullptr;
    unsigned long long injected = 0, recorded = 0;
    NvlIslands isl;
    vector<FlowInfo> fi_host;
    vector<int> h_srv_host;
    int64_t isl_lim = 0;
    void setup(Sim &s, int span_slots, const vector<FlowInfo> &fi, const vector<int> &h_srv)
    {
        fast.resize(fi.size());
        for (size_t g = 0; g < fi.size(); g++)
            fast[g] = fi[g].fast;
        s.nvl_oracle = !g_nvl_oracle.empty();
        if (!g_nvl_record.empty() && !(rec_f = fopen(g_nvl_record.c_str(), "wb")))
        {
            fprintf(stderr, "cannot write %s\n", g_nvl_record.c_str());
            exit(1);
        }
        if (rec_f)
        {
            if (!(st_f = fopen((g_nvl_record + ".starts").c_str(), "wb")))
            {
                fprintf(stderr, "cannot write %s.starts\n", g_nvl_record.c_str());
                exit(1);
            }
            s.nvl_xcap = 1 << 20;
            s.nvl_xc = dev_alloc<Chain>(s.nvl_xcap);
            s.nvl_xh = dev_alloc<CEvH>(s.nvl_xcap);
            s.nvl_xa = dev_alloc<int64_t>(s.nvl_xcap);
            s.nvl_xn = dev_alloc<unsigned long long>(1);
            for (auto &p : g_nvl_pre)
            {
                NvlRec r = {};
                r.tag = 3; // (its chain is main's: no tail)
                r.h.t = p.ts;
                r.h.flow = p.g;
                r.c = p.c;
                fwrite(&r, sizeof(r), 1, st_f);
            }
        }
        if (g_nvl_islands)
        {
            s.nvl_oracle = 1; // (the GPU does not run them)
            s.nvl_xcap = 1 << 20;
            s.nvl_xc = dev_alloc<Chain>(s.nvl_xcap);
            s.nvl_xh = dev_alloc<CEvH>(s.nvl_xcap);
            s.nvl_xa = dev_alloc<int64_t>(s.nvl_xcap);
            s.nvl_xn = dev_alloc<unsigned long long>(1);
            fi_host = fi;
            h_srv_host = h_srv;
            isl.start(s, fi, h_srv, topo, s.P.delay, g_nvl_islands);
            isl.pre(g_nvl_pre, fi, h_srv);
        }
        if (!g_nvl_oracle.empty())
        {
            FILE *f = fopen(g_nvl_oracle.c_str(), "rb");
            if (!f)
            {
                fprintf(stderr, "cannot read %s\n", g_nvl_oracle.c_str());
                exit(1);
            }
            NvlRec r;
            while (fread(&r, sizeof(r), 1, f) == 1)
                orecs.push_back(r);
            fclose(f);
        }
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
        if (onext < orecs.size())
            t = std::min(t, orecs[onext].avail); // (no later item of the recording is due before it)
        if (isl.n && isl.live > 0)
            return std::min(t / (D ? D : 25), isl.slot); // (the islands may complete a flow any time)
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
        isl_lim = span;
        vector<vector<NvlRec>> isl_out, isl_more;
        for (;;)
        {
            if (isl.n)
                isl.go(isl_lim, st); // (the islands run alongside the GPU's launches)
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
                fprintf(stderr, "sync %llu slot %lld k %d completions %llu acts %zu gpu %.1f s cpu %.1f s sync %.1f s; idle %d cap %lld limit %lld timer %d pool %zu ready %zu; islands wait %.1f s move %.1f s live %lld nodes %zu\n", syncs, (long long)S, ctl.k, ctl.head, acts.size(), t_gpu, t_cpu, t_sync, ctl.idle, (long long)ctl.cap, (long long)ctl.limit, (int)fe->has_timer(), pool.size(), ready.size(), isl.t_wait, isl.t_xfer, isl.live, isl.gnodes.size());
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
            if (rec_f)
            {
                record(s, st, hs, seen, ctl.head, rec_reach);
                record_starts(s, st, rec_reach);
            }
            if (g_nvl_cnt_host)
            {
                // the events inside each server since the last sync, for --nvl-record's .ev file
                static FILE *evf = fopen((g_nvl_record + ".ev").c_str(), "wb");
                static vector<uint32_t> cnt;
                cnt.resize(g_nvl_nsw);
                CK(hipMemcpy(cnt.data(), g_nvl_cnt_host, g_nvl_nsw * sizeof(uint32_t), hipMemcpyDeviceToHost));
                CK(hipMemset(g_nvl_cnt_host, 0, g_nvl_nsw * sizeof(uint32_t)));
                const int64_t at = S * D;
                fwrite(&at, sizeof(at), 1, evf);
                fwrite(cnt.data(), sizeof(uint32_t), g_nvl_nsw, evf);
                fflush(evf);
            }
            seen = ctl.head;
            // the due ones in order
            const int64_t reach = S * D;
            rec_reach = reach;
            if (isl.n)
            {
                isl.collect(isl_out);
                if (S > isl.sent_limit)
                {
                    // (the GPU went on alone, with no island flow running: the islands catch up)
                    isl.go(S, st);
                    isl.collect(isl_more);
                    for (int k = 0; k < isl.n; k++)
                        isl_out[k].insert(isl_out[k].end(), isl_more[k].begin(), isl_more[k].end());
                }
                seen = isl.inject(s, st, isl_out, dev_h, seen, cev_mask, [&](const CEvH &h, uint32_t j) { pool.push_back(Pending{h.t, j, h, 0}); });
            }
            if (onext < orecs.size() && orecs[onext].avail < reach)
            {
                // the recording's items the GPU has now passed: tail nodes into this run's ring,
                // completions into the ring of completions, as the GPU's own
                unsigned long long head = ctl.head;
                TailPool tp;
                CK(hipMemcpyFromSymbol(&tp, HIP_SYMBOL(g_tail), sizeof(tp)));
                unsigned long long tcnt;
                CK(hipMemcpy(&tcnt, tp.cnt, sizeof(tcnt), hipMemcpyDeviceToHost));
                vector<TailNode> nodes;
                const unsigned long long t0 = tcnt;
                auto flush = [&]() {
                    for (size_t q = 0; q < nodes.size();)
                    {
                        const uint64_t slot = (t0 + q) & tp.mask, m = std::min<uint64_t>(nodes.size() - q, tp.mask + 1 - slot);
                        CK(hipMemcpyAsync(tp.n + slot, &nodes[q], m * sizeof(TailNode), hipMemcpyHostToDevice, st));
                        q += m;
                    }
                };
                for (; onext < orecs.size() && orecs[onext].avail < reach; onext++)
                {
                    const NvlRec &r = orecs[onext];
                    if (r.tag == 0)
                    {
                        TailNode nd = r.n;
                        nd.pos = ++tcnt;
                        auto it = opos.find(nd.next);
                        // (not recorded: overwritten in the recording's ring, or beyond NVL_DEPTH; a pos no
                        // node will have, so that a comparison reaching it counts as on a lost tail)
                        nd.next = !nd.next ? 0 : it != opos.end() ? it->second : (1ull << 55);
                        opos[r.n.pos] = nd.pos;
                        nodes.push_back(nd);
                        continue;
                    }
                    const unsigned long long j = head & cev_mask;
                    Chain c = r.c;
                    auto it = opos.find(c.tail);
                    c.tail = !c.tail ? 0 : it != opos.end() ? it->second : (1ull << 55);
                    CK(hipMemcpyAsync(dev_h + j, &r.h, sizeof(CEvH), hipMemcpyHostToDevice, st));
                    CK(hipMemcpyAsync(s.cev_c + j, &c, sizeof(Chain), hipMemcpyHostToDevice, st));
                    pool.push_back(Pending{r.h.t, (uint32_t)j, r.h, 0});
                    injected++;
                    head++;
                }
                flush();
                CK(hipMemcpyAsync(tp.cnt, &tcnt, sizeof(tcnt), hipMemcpyHostToDevice, st));
                CK(hipMemcpyAsync(&s.cl->head, &head, sizeof(head), hipMemcpyHostToDevice, st));
                CK(hipStreamSynchronize(st));
                seen = head;
            }
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
            if (ctl.idle && acts.empty() && pool.empty() && ready.empty() && !fe->has_timer() && onext == orecs.size() && isl.live == 0)
                return;
            apply(st, S + span, seen);
            isl_lim = S + span;
            for (int t : tfree_later)
                tfree.push_back(t);
            if (isl.n)
                isl.take_starts(s, st, fi_host, h_srv_host, reach);
            CK(hipStreamSynchronize(st));
            t_sync += sec(e, now());
        }
    }
    // the flows within servers: their starts and completion times kept (--nvl-record) or put back
    template <typename FI>
    void nvl_fct(vector<FI> &fi, vector<int64_t> &fct, unsigned int &done)
    {
        if (!g_nvl_record.empty())
        {
            FILE *f = fopen((g_nvl_record + ".fct").c_str(), "w");
            for (size_t g = 0; g < fi.size(); g++)
                if (fast[g] && fct[g] >= 0)
                    fprintf(f, "%zu %lld %lld %d %d %llu\n", g, (long long)fi[g].start, (long long)fct[g], fi[g].src, fi[g].dst,
                            (unsigned long long)fi[g].size);
            fclose(f);
        }
        if (isl.n)
        {
            vector<int64_t> st(fi.size(), -1);
            isl.finish(st, fct, done);
            for (size_t g = 0; g < fi.size(); g++)
                if (st[g] >= 0)
                    fi[g].start = st[g];
        }
        if (!g_nvl_oracle.empty())
        {
            FILE *f = fopen((g_nvl_oracle + ".fct").c_str(), "r");
            if (!f)
            {
                fprintf(stderr, "cannot read %s.fct\n", g_nvl_oracle.c_str());
                exit(1);
            }
            size_t g;
            long long a, b;
            char line[256];
            while (fgets(line, sizeof(line), f) && sscanf(line, "%zu %lld %lld", &g, &a, &b) == 3)
                if (g < fi.size() && fast[g] && fct[g] < 0)
                {
                    fi[g].start = a;
                    fct[g] = b;
                    done++;
                }
            fclose(f);
        }
    }
    // --nvl-record: the new completions of flows within servers, with the tail nodes their chains
    // reach that were not recorded before (found on the GPU)
    unsigned long long rec_tails = 0;
    int64_t rec_reach = 0;
    void record(Sim &s, hipStream_t st, const vector<CEvH> &hs, unsigned long long from, unsigned long long to, int64_t avail)
    {
        vector<uint32_t> idx;
        vector<unsigned long long> at;
        for (unsigned long long i = from; i < to; i++)
            if (fast[hs[i - from].flow])
            {
                idx.push_back((uint32_t)(i & cev_mask));
                at.push_back(i);
            }
        if (idx.empty())
            return;
        const int n = idx.size(), cap = 1 << 22;
        static uint32_t *d_idx = nullptr;
        static int *d_seg = nullptr, *d_k = nullptr, d_n = 0;
        static TailNode *d_out = nullptr;
        static uint64_t *d_mark = nullptr;
        if (!d_out)
        {
            TailPool tp;
            CK(hipMemcpyFromSymbol(&tp, HIP_SYMBOL(g_tail), sizeof(tp)));
            d_mark = dev_alloc<uint64_t>(tp.mask + 1);
            d_out = dev_alloc<TailNode>(cap);
            d_k = dev_alloc<int>(2);
        }
        if (n > d_n)
        {
            if (d_idx)
            {
                CK(hipFree(d_idx));
                CK(hipFree(d_seg));
            }
            d_n = std::max(n, 2 * d_n);
            d_idx = dev_alloc<uint32_t>(d_n);
            d_seg = dev_alloc<int>(d_n + 1);
        }
        CK(hipMemcpy(d_idx, idx.data(), n * sizeof(uint32_t), hipMemcpyHostToDevice));
        nvl_walk<<<1, 1, 0, st>>>(s.cev_c, d_idx, n, d_out, cap, d_seg, d_k, d_mark, d_k + 1, NVL_DEPTH);
        CK(hipStreamSynchronize(st));
        int kk[2];
        CK(hipMemcpy(kk, d_k, sizeof(kk), hipMemcpyDeviceToHost));
        if (kk[1])
        {
            fprintf(stderr, "nvl: more than %d new tail nodes in a sync\n", cap);
            exit(1);
        }
        vector<int> seg(n + 1);
        vector<TailNode> nodes(kk[0]);
        CK(hipMemcpy(seg.data(), d_seg, (n + 1) * sizeof(int), hipMemcpyDeviceToHost));
        if (kk[0])
            CK(hipMemcpy(nodes.data(), d_out, kk[0] * sizeof(TailNode), hipMemcpyDeviceToHost));
        for (int e = 0; e < n; e++)
        {
            NvlRec r = {};
            r.avail = avail;
            for (int q = seg[e + 1] - 1; q >= seg[e]; q--) // (oldest first: each points to one already written)
            {
                r.tag = 0;
                r.n = nodes[q];
                fwrite(&r, sizeof(r), 1, rec_f);
                rec_tails++;
            }
            r = {};
            r.tag = 1;
            r.avail = avail;
            r.h = hs[at[e] - from];
            CK(hipMemcpy(&r.c, s.cev_c + idx[e], sizeof(Chain), hipMemcpyDeviceToHost));
            fwrite(&r, sizeof(r), 1, rec_f);
            recorded++;
        }
    }
    // --nvl-record: the messages made since the last sync to start flows within servers (.starts:
    // tag 2, avail its bucket's slot, n.pos the time it was made at), with their chains' new tail nodes
    FILE *st_f = nullptr;
    unsigned long long st_seen = 0, st_written = 0, st_tails = 0;
    void record_starts(Sim &s, hipStream_t st, int64_t made)
    {
        unsigned long long xn;
        CK(hipMemcpy(&xn, s.nvl_xn, sizeof(xn), hipMemcpyDeviceToHost));
        if (xn - st_seen > s.nvl_xcap)
        {
            fprintf(stderr, "nvl: more than %llu messages to record in a sync\n", s.nvl_xcap);
            exit(1);
        }
        if (xn == st_seen)
            return;
        const int n = (int)(xn - st_seen), cap = 1 << 22;
        vector<uint32_t> idx(n);
        for (int e = 0; e < n; e++)
            idx[e] = (uint32_t)((st_seen + e) & (s.nvl_xcap - 1));
        static uint32_t *d_idx = nullptr;
        static int *d_seg = nullptr, *d_k = nullptr, d_n = 0;
        static TailNode *d_out = nullptr;
        static uint64_t *d_mark = nullptr;
        if (!d_out)
        {
            TailPool tp;
            CK(hipMemcpyFromSymbol(&tp, HIP_SYMBOL(g_tail), sizeof(tp)));
            d_mark = dev_alloc<uint64_t>(tp.mask + 1);
            d_out = dev_alloc<TailNode>(cap);
            d_k = dev_alloc<int>(2);
        }
        if (n > d_n)
        {
            if (d_idx)
            {
                CK(hipFree(d_idx));
                CK(hipFree(d_seg));
            }
            d_n = std::max(n, 2 * d_n);
            d_idx = dev_alloc<uint32_t>(d_n);
            d_seg = dev_alloc<int>(d_n + 1);
        }
        CK(hipMemcpy(d_idx, idx.data(), n * sizeof(uint32_t), hipMemcpyHostToDevice));
        nvl_walk<<<1, 1, 0, st>>>(s.nvl_xc, d_idx, n, d_out, cap, d_seg, d_k, d_mark, d_k + 1, NVL_DEPTH);
        CK(hipStreamSynchronize(st));
        int kk[2];
        CK(hipMemcpy(kk, d_k, sizeof(kk), hipMemcpyDeviceToHost));
        if (kk[1])
        {
            fprintf(stderr, "nvl: more than %d new tail nodes in a sync\n", cap);
            exit(1);
        }
        vector<int> seg(n + 1);
        vector<TailNode> nodes(kk[0]);
        CK(hipMemcpy(seg.data(), d_seg, (n + 1) * sizeof(int), hipMemcpyDeviceToHost));
        if (kk[0])
            CK(hipMemcpy(nodes.data(), d_out, kk[0] * sizeof(TailNode), hipMemcpyDeviceToHost));
        vector<Chain> cs(n);
        vector<CEvH> hs(n);
        vector<int64_t> as(n);
        for (int q = 0; q < n;)
        {
            const unsigned long long j = (st_seen + q) & (s.nvl_xcap - 1), c = std::min<unsigned long long>(n - q, s.nvl_xcap - j);
            CK(hipMemcpy(cs.data() + q, s.nvl_xc + j, c * sizeof(Chain), hipMemcpyDeviceToHost));
            CK(hipMemcpy(hs.data() + q, s.nvl_xh + j, c * sizeof(CEvH), hipMemcpyDeviceToHost));
            CK(hipMemcpy(as.data() + q, s.nvl_xa + j, c * sizeof(int64_t), hipMemcpyDeviceToHost));
            q += c;
        }
        for (int e = 0; e < n; e++)
        {
            NvlRec r = {};
            for (int q = seg[e + 1] - 1; q >= seg[e]; q--)
            {
                r.tag = 0;
                r.n = nodes[q];
                fwrite(&r, sizeof(r), 1, st_f);
                st_tails++;
            }
            r = {};
            r.tag = 2;
            r.avail = as[e];
            r.h = hs[e];
            r.c = cs[e];
            r.n.pos = (uint64_t)made;
            fwrite(&r, sizeof(r), 1, st_f);
            st_written++;
        }
        st_seen = xn;
    }
    const bool progress = getenv("TWINS_AI_PROGRESS") != nullptr;
    bool single = false;
    void apply(hipStream_t st, int64_t limit, unsigned long long seen)
    {
        if (!acts.empty())
            CK(hipMemcpyAsync(dev_act, acts.data(), acts.size() * sizeof(CAct), hipMemcpyHostToDevice, st));
        for (auto &x : acts)
        {
            const bool own = !(x.child & DACT_TIMER) && fast[x.child] && (!orecs.empty() || isl.n);
            single |= !(x.child & DACT_TIMER) && !own; // (a flow of a server's own: elsewhere)
            if (own && isl.n)
                isl.live++; // (counted before next_cap: no idle jump past what the islands may make)
        }
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
        if (st_f)
        {
            fclose(st_f);
            printf("nvl: %llu messages to flows within servers recorded, %llu tail nodes\n", st_written, st_tails);
        }
        if (rec_f)
        {
            fclose(rec_f);
            printf("nvl: %llu completions of flows within servers recorded, %llu tail runs\n", recorded, rec_tails);
        }
        if (!orecs.empty())
            printf("nvl: %llu of %zu recorded completions put in (flows within servers not run)\n", injected, orecs.size());
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
