// twins-island: one server's NVLink part on a CPU core (docs/ai/speed.md, lever C). rstep.cuh's
// device code, built for the CPU (TWINS_CPU: one lane a block, blocks in turn, switches serial),
// steps the server's GPUs and NVSwitch slot by slot, as the GPU would. Its flows start from
// messages put into its buckets with their chains (as dag_msg files them), and its completions
// are written out with their chains, in the format of twins-ai --nvl-record (closed.h, NvlRec).
//
// Offline (a test against a recording): --isl-in=F is twins-ai --nvl-record's .starts file (the
// messages to every server's flows, with the tail nodes their chains reach), --isl-map=M the
// global flow id of each of this island's flows (its .dag lists them in that order), and
// --isl-out=O receives the completions. Included in rmain.cu.
#pragma once
#include <unordered_map>

static string g_isl_in, g_isl_map, g_isl_out;

// every flow waits for one message; nothing else starts it
static void island_world(World &w)
{
    w.traced = true;
    w.acts.assign(2 * w.fi.size(), {});
    w.ndeps.assign(w.fi.size(), 1);
}

static void island_setup(Sim &s)
{
    s.cl_span = (int)((s.send_lat - 1) / s.P.delay);
    s.cev_mask = (1ull << 20) - 1;
    s.cev_h = dev_alloc<CEvH>(s.cev_mask + 1);
    s.cev_c = dev_alloc<Chain>(s.cev_mask + 1);
    s.cl = dev_alloc<ClCtl>(1);
    s.cl->limit = INT64_MAX / 2;
    s.cl->cap = INT64_MAX;
}

struct NvlRec
{
    uint32_t tag, pad;
    int64_t avail;
    CEvH h;
    Chain c;
    TailNode n;
};

static void island_run(Sim &s, int64_t D)
{
    // this island's flows: local id -> global, and back
    vector<int> glob;
    std::unordered_map<int, int> loc;
    {
        FILE *f = fopen(g_isl_map.c_str(), "r");
        if (!f)
        {
            fprintf(stderr, "cannot read %s\n", g_isl_map.c_str());
            exit(1);
        }
        int g;
        while (fscanf(f, "%d", &g) == 1)
        {
            loc[g] = (int)glob.size();
            glob.push_back(g);
        }
        fclose(f);
    }
    // the recording: tail nodes (by their pos there) and this island's starts
    struct Node
    {
        uint32_t code, cnt;
        uint64_t next;
    };
    std::unordered_map<uint64_t, Node> rnodes;
    struct Start
    {
        int64_t made, ts, a;
        int f;
        Chain c;
    };
    vector<Start> starts;
    {
        FILE *f = fopen(g_isl_in.c_str(), "rb");
        if (!f)
        {
            fprintf(stderr, "cannot read %s\n", g_isl_in.c_str());
            exit(1);
        }
        NvlRec r;
        while (fread(&r, sizeof(r), 1, f) == 1)
        {
            if (r.tag == 0)
                rnodes[r.n.pos] = Node{r.n.code, r.n.cnt, r.n.next};
            else if (r.tag == 3)
            {
                // main's: ready at the start, as rmain files them
                auto it = loc.find(r.h.flow);
                if (it != loc.end())
                {
                    const int g = it->second, h = s.fi[g].src;
                    s.dep_t[g] = r.h.t;
                    s.dep_c[g] = r.c;
                    s.dyn[(size_t)h * s.dyn_cap + s.dyn_n[h]++] = g;
                }
            }
            else if (r.tag == 2)
            {
                auto it = loc.find(r.h.flow);
                if (it != loc.end())
                    starts.push_back(Start{(int64_t)r.n.pos, r.h.t, r.avail, it->second, r.c});
            }
        }
        fclose(f);
    }
    std::stable_sort(starts.begin(), starts.end(), [](const Start &a, const Start &b) { return a.made < b.made; });
    // the recording's tail nodes, rebuilt in this ring when a chain reaches them (oldest first)
    TailPool tp = g_tail;
    std::unordered_map<uint64_t, uint64_t> mine; // recording's pos -> pos here
    auto rebuild = [&](uint64_t p) -> uint64_t {
        vector<uint64_t> todo;
        uint64_t q = p;
        while (q && !mine.count(q) && rnodes.count(q))
        {
            todo.push_back(q);
            q = rnodes[q].next;
        }
        for (size_t i = todo.size(); i-- > 0;)
        {
            const Node &n = rnodes[todo[i]];
            const uint64_t pos = ++*tp.cnt;
            uint64_t nx = 0;
            if (n.next)
            {
                auto it = mine.find(n.next);
                nx = it != mine.end() ? it->second : (1ull << 62); // (not recorded: lost, if reached)
            }
            tp.n[(pos - 1) & tp.mask] = TailNode{n.code, n.cnt, nx, pos};
            mine[todo[i]] = pos;
        }
        if (!p)
            return 0;
        auto it = mine.find(p);
        return it != mine.end() ? it->second : (1ull << 62);
    };
    // completions out, with the tail nodes of this ring their chains reach (each once)
    FILE *out = fopen(g_isl_out.c_str(), "wb");
    if (!out)
    {
        fprintf(stderr, "cannot write %s\n", g_isl_out.c_str());
        exit(1);
    }
    std::unordered_map<uint64_t, char> sent;
    unsigned long long seen = 0, written = 0;
    auto flush = [&]() {
        ClCtl &cl = *s.cl;
        for (; seen < cl.head; seen++)
        {
            const unsigned long long j = seen & s.cev_mask;
            const Chain &c = s.cev_c[j];
            vector<TailNode> nn;
            for (uint64_t p = c.tail; p && !sent.count(p);)
            {
                const TailNode &t = tp.n[(p - 1) & tp.mask];
                if (t.pos != p)
                    break;
                nn.push_back(t);
                sent[p] = 1;
                p = t.next;
            }
            NvlRec r = {};
            for (size_t i = nn.size(); i-- > 0;)
            {
                r.tag = 0;
                r.n = nn[i];
                fwrite(&r, sizeof(r), 1, out);
            }
            r = {};
            r.tag = 1;
            r.h = s.cev_h[j];
            r.h.flow = glob[r.h.flow];
            r.c = c;
            fwrite(&r, sizeof(r), 1, out);
            written++;
        }
        cl.seen = cl.floor = cl.head;
    };
    const BlockInfo *tab = s.bi_s;
    const int nb = s.n1 + s.ns_s + s.nm;
    vector<char> smem(1 << 20);
    blockDim.x = 1;
    threadIdx.x = 0;
    size_t si = 0;
    auto t0 = std::chrono::steady_clock::now();
    unsigned long long steps = 0;
    for (;;)
    {
        const int64_t slot = *s.slot;
        // the messages made by now (their makers ran at this slot or before)
        for (; si < starts.size() && starts[si].made / D <= slot; si++)
        {
            const Start &x = starts[si];
            if (x.a <= slot)
            {
                fprintf(stderr, "island: a message for slot %lld made at %lld\n", (long long)x.a, (long long)slot);
                exit(1);
            }
            const int b = (int)fmod(s.fnbi, x.a);
            const unsigned n = s.dmsg_n[b]++;
            if (n >= (unsigned)s.dcap)
            {
                fprintf(stderr, "island: DAG message overflow\n");
                exit(1);
            }
            DMsg &m = s.dmsg[(size_t)b * s.dcap + n];
            m.c = x.c;
            m.c.tail = rebuild(x.c.tail);
            m.ts = x.ts;
            m.flow = x.f;
            const FlowInfo &cf = s.fi[x.f];
            m.next = s.hhead[(size_t)cf.src * s.nbi + b];
            s.hhead[(size_t)cf.src * s.nbi + b] = n + 1;
            s.pend[fmod(s.fpend, x.a)] = 1;
        }
        // (an idle jump stops at the next message's making)
        s.cl->cap = si < starts.size() ? starts[si].made / D : INT64_MAX;
        if (*s.finished || (si == starts.size() && *s.flows_done >= (unsigned)s.F))
            break;
        if (si == starts.size() && s.cl->idle)
            break;
        for (int b = 0; b < nb; b++)
        {
            blockIdx.x = b;
            slot_entity<false>(s, b, tab[b], slot, smem.data(), 1);
        }
        steps++;
        flush();
    }
    flush();
    fclose(out);
    const double sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    printf("island: %zu messages put in, %llu completions out, %llu slots stepped, %.3f s\n", starts.size(), written, steps, sec);
}
