// twins-island: one server's NVLink part on a CPU core (docs/ai/speed.md, lever C). rstep.cuh's
// device code, built for the CPU (TWINS_CPU: one lane a block, blocks in turn, switches serial),
// steps the server's GPUs and NVSwitch slot by slot, as the GPU would. Its flows start from
// messages put into its buckets with their chains (as dag_msg files them), and its completions
// go out with their chains (src/rdma/isl_shm.h's items).
//
// With twins-ai (--isl-shm=NAME): commands and items through a shared-memory segment.
// Offline, a test against a recording: --isl-in=F is twins-ai --nvl-record's .starts file (the
// messages to every server's flows), --isl-out=O receives the completions in --nvl-record's format.
// Either way, --isl-map=M lists the global flow id of each of this island's flows (its .dag has
// them in that order). Included in rmain.cu.
#pragma once
#include <unordered_map>
#include "rdma/isl_shm.h"

static string g_isl_in, g_isl_map, g_isl_out, g_isl_shm, g_isl_rings;
static int g_isl_side = 0, g_isl_nrings = 0, g_isl_ringlog = 22;
// the GPU's tail nodes this island was sent, by their pos there; the other islands' rings
static std::unordered_map<uint64_t, TailNode> g_isl_copies;
static vector<const TailNode *> g_isl_ring;
static TailNode g_isl_none = {};
static const IslCopies *g_isl_cp = nullptr; // with twins-ai: its table of the GPU's nodes (isl_shm.h)
static uint32_t g_isl_cp_gen = 0;
namespace rdma
{
uint64_t g_tail_side = 0;
TailNode island_remote(uint64_t p)
{
    const int k = (int)(p >> TAIL_SIDE);
    if (k == 0)
    {
        if (g_isl_cp)
        {
            const TailNode *t = g_isl_cp->find(p);
            return t ? *t : g_isl_none; // (not there: lost, if reached)
        }
        auto it = g_isl_copies.find(p);
        return it != g_isl_copies.end() ? it->second : g_isl_none; // (not sent: lost, if reached)
    }
    if (k <= (int)g_isl_ring.size() && g_isl_ring[k - 1])
    {
        // (another island's, being written: see tail_at)
        const TailNode &v = g_isl_ring[k - 1][(p - 1) & ((1ull << g_isl_ringlog) - 1)];
        TailNode n;
        n.pos = __atomic_load_n(&v.pos, __ATOMIC_ACQUIRE);
        n.code = __atomic_load_n(&v.code, __ATOMIC_RELAXED);
        n.cnt = __atomic_load_n(&v.cnt, __ATOMIC_RELAXED);
        n.next = __atomic_load_n(&v.next, __ATOMIC_RELAXED);
        __atomic_thread_fence(__ATOMIC_ACQUIRE);
        if (__atomic_load_n(&v.pos, __ATOMIC_RELAXED) != n.pos)
            n.pos = 0;
        return n;
    }
    return g_isl_none;
}
} // namespace rdma

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

struct Island
{
    Sim &s;
    int64_t D;
    vector<int> glob;                   // local flow -> global
    std::unordered_map<int, int> loc;   // and back
    unsigned long long seen = 0;
    const BlockInfo *tab;
    int nb;
    vector<char> smem;
    unsigned long long steps = 0, puts = 0;

    Island(Sim &s_, int64_t D_) : s(s_), D(D_), tab(s_.bi_s), nb(s_.n1 + s_.ns_s + s_.nm), smem(1 << 20)
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
        blockDim.x = 1;
        threadIdx.x = 0;
        // (offline: this ring as it is, the side 0's numbering; with twins-ai: the shared rings)
    }
    // an item from the sender; false: a start for another island's flow
    bool take(const NvlRec &r)
    {
        if (r.tag == 0)
        {
            g_isl_copies[r.n.pos] = r.n; // (a GPU node)
            return true;
        }
        auto it = loc.find(r.h.flow);
        if (it == loc.end())
            return false;
        const int f = it->second, h = s.fi[f].src;
        if (r.tag == 3)
        {
            // main's: ready at the start, as rmain files them
            s.dep_t[f] = r.h.t;
            s.dep_c[f] = r.c;
            s.dyn[(size_t)h * s.dyn_cap + s.dyn_n[h]++] = f;
            return true;
        }
        // as dag_msg files it
        const int64_t a = r.avail;
        if (a < *s.slot)
        {
            fprintf(stderr, "island: a message for slot %lld at slot %lld\n", (long long)a, (long long)*s.slot);
            exit(1);
        }
        const int b = (int)fmod(s.fnbi, a);
        const unsigned n = s.dmsg_n[b]++;
        if (n >= (unsigned)s.dcap)
        {
            fprintf(stderr, "island: DAG message overflow\n");
            exit(1);
        }
        DMsg &m = s.dmsg[(size_t)b * s.dcap + n];
        m.c = r.c;
        m.ts = r.h.t;
        m.flow = f;
        m.next = s.hhead[(size_t)h * s.nbi + b];
        s.hhead[(size_t)h * s.nbi + b] = n + 1;
        s.pend[fmod(s.fpend, a)] = 1;
        puts++;
        return true;
    }
    // one slot of every block
    void step()
    {
        const int64_t slot = *s.slot;
        for (int b = 0; b < nb; b++)
        {
            blockIdx.x = b;
            slot_entity<false>(s, b, tab[b], slot, smem.data(), 1);
        }
        steps++;
    }
    // the completions made since the last call (their chains' tails are this island's own nodes, read
    // where they are, or nodes others made)
    template <typename F>
    void completions(F &&emit)
    {
        ClCtl &cl = *s.cl;
        for (; seen < cl.head; seen++)
        {
            const unsigned long long j = seen & s.cev_mask;
            NvlRec r = {};
            r.tag = 1;
            r.h = s.cev_h[j];
            r.h.flow = glob[r.h.flow];
            r.c = s.cev_c[j];
            emit(r);
        }
        cl.seen = cl.floor = cl.head;
    }
};

// offline: the recording's messages to this island's flows, each put in when its maker ran
static void island_offline(Island &is)
{
    Sim &s = is.s;
    g_tail_side = 1ull << TAIL_SIDE; // (this island's nodes apart from the recording's)
    *g_tail.cnt = g_tail_side;
    struct Start
    {
        int64_t made;
        NvlRec r;
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
            if (r.tag == 0 || r.tag == 3)
                is.take(r);
            else if (r.tag == 2 && is.loc.count(r.h.flow))
                starts.push_back(Start{(int64_t)r.n.pos, r});
        }
        fclose(f);
    }
    std::stable_sort(starts.begin(), starts.end(), [](const Start &a, const Start &b) { return a.made < b.made; });
    FILE *out = fopen(g_isl_out.c_str(), "wb");
    if (!out)
    {
        fprintf(stderr, "cannot write %s\n", g_isl_out.c_str());
        exit(1);
    }
    unsigned long long written = 0;
    auto emit = [&](const NvlRec &r) {
        fwrite(&r, sizeof(r), 1, out);
        written += r.tag == 1;
    };
    size_t si = 0;
    auto t0 = std::chrono::steady_clock::now();
    for (;;)
    {
        for (; si < starts.size() && starts[si].made / is.D <= *s.slot; si++)
            is.take(starts[si].r);
        s.cl->cap = si < starts.size() ? starts[si].made / is.D : INT64_MAX; // (an idle jump stops there)
        if (*s.finished || (si == starts.size() && (*s.flows_done >= (unsigned)s.F || s.cl->idle)))
            break;
        is.step();
        is.completions(emit);
    }
    is.completions(emit);
    fclose(out);
    printf("island: %zu messages put in, %llu completions out, %llu slots stepped, %.3f s\n", starts.size(), written, is.steps,
           std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count());
}

// with twins-ai: commands through the segment
static void island_live(Island &is)
{
    Sim &s = is.s;
    IslShm *m = isl_map(g_isl_shm.c_str(), false);
    if (!m)
    {
        fprintf(stderr, "island: cannot open %s\n", g_isl_shm.c_str());
        exit(1);
    }
    {
        const size_t bytes = sizeof(TailNode) << g_isl_ringlog;
        g_isl_ring.assign(g_isl_nrings, nullptr);
        for (int k = 0; k < g_isl_nrings; k++)
        {
            const string name = g_isl_rings + std::to_string(k);
            int fd = shm_open(name.c_str(), k + 1 == g_isl_side ? O_RDWR : O_RDONLY, 0600);
            void *p = fd < 0 ? MAP_FAILED : mmap(nullptr, bytes, k + 1 == g_isl_side ? PROT_READ | PROT_WRITE : PROT_READ, MAP_SHARED, fd, 0);
            if (fd >= 0)
                close(fd);
            if (p == MAP_FAILED)
            {
                fprintf(stderr, "island: cannot map %s\n", name.c_str());
                exit(1);
            }
            g_isl_ring[k] = static_cast<const TailNode *>(p);
            if (k + 1 == g_isl_side)
            {
                g_tail.n = static_cast<TailNode *>(p);
                g_tail.mask = (1ull << g_isl_ringlog) - 1;
                *g_tail.cnt = (uint64_t)g_isl_side << TAIL_SIDE;
            }
        }
        g_tail_side = (uint64_t)g_isl_side << TAIL_SIDE;
    }
    uint32_t seq = 0;
    vector<NvlRec> pend; // what did not fit out[]
    size_t pend_at = 0;
    auto send = [&]() {
        uint64_t n = 0;
        NvlRec *o = m->out();
        while (pend_at < pend.size() && n < m->out_cap)
            o[n++] = pend[pend_at++];
        m->out_n = n;
        m->more = pend_at < pend.size();
        if (!m->more)
        {
            pend.clear();
            pend_at = 0;
        }
    };
    for (;;)
    {
        isl_wait(m->cmd_seq, seq);
        seq = m->cmd_seq.load(std::memory_order_acquire);
        const uint32_t cmd = m->cmd;
        if (cmd == ISL_QUIT)
        {
            m->slot = *s.slot;
            isl_wake(m->done_seq);
            return;
        }
        if (cmd == ISL_MORE)
        {
            send();
            isl_wake(m->done_seq);
            continue;
        }
        if (m->copy_gen != g_isl_cp_gen)
        {
            // (twins-ai's table of the GPU's nodes, new or grown)
            if (g_isl_cp)
                munmap((void *)g_isl_cp, IslCopies::bytes(g_isl_cp->mask + 1));
            int fd = shm_open(m->copy_name, O_RDONLY, 0600);
            IslCopies h;
            void *p = fd >= 0 && pread(fd, &h, sizeof(h), 0) == (ssize_t)sizeof(h)
                          ? mmap(nullptr, IslCopies::bytes(h.mask + 1), PROT_READ, MAP_SHARED, fd, 0)
                          : MAP_FAILED;
            if (fd >= 0)
                close(fd);
            if (p == MAP_FAILED)
            {
                fprintf(stderr, "island: cannot map %s\n", m->copy_name);
                exit(1);
            }
            g_isl_cp = static_cast<const IslCopies *>(p);
            g_isl_cp_gen = m->copy_gen;
        }
        NvlRec *in = m->in();
        for (uint64_t i = 0; i < m->in_n; i++)
            is.take(in[i]);
        if (cmd == ISL_RUN)
        {
            auto t0 = std::chrono::steady_clock::now();
            s.cl->cap = m->limit; // (an idle jump stops at the limit: what comes next is not known yet)
            while (*s.slot < m->limit && !*s.finished)
            {
                is.step();
                if (s.cl->head != is.seen) // (idle jumps wait for the completions to be read)
                    is.completions([&](const NvlRec &r) { pend.push_back(r); });
            }
            is.completions([&](const NvlRec &r) { pend.push_back(r); });
            m->busy_s += std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
            send();
        }
        else
        {
            m->out_n = 0;
            m->more = 0;
        }
        m->slot = *s.slot;
        m->nodes = *g_tail.cnt - g_tail_side;
        memcpy(m->ev, s.ev, sizeof(m->ev));
        isl_wake(m->done_seq);
    }
}

static void island_run(Sim &s, int64_t D)
{
    Island is(s, D);
    if (!g_isl_shm.empty())
        island_live(is);
    else
        island_offline(is);
}
